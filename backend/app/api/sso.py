import asyncio
import base64
import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
import jwt
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.security import issue_token, passwords
from app.models import ExternalIdentity, OAuthAttempt, User

router = APIRouter(prefix="/api/v1/auth/sso", tags=["SSO"])


def sha(value):
    return hashlib.sha256(value.encode()).hexdigest()


async def discovery(client, settings):
    if not settings.oidc_issuer:
        raise HTTPException(503, "未配置OIDC单点登录")
    response = await client.get(settings.oidc_issuer.rstrip("/") + "/.well-known/openid-configuration")
    response.raise_for_status()
    data = response.json()
    if data.get("issuer") != settings.oidc_issuer:
        raise ValueError("issuer mismatch")
    for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
        target = urlsplit(data[key])
        if target.scheme != "https" or not target.hostname or target.username or target.fragment:
            raise ValueError("invalid discovery endpoint")
    return data


@router.get("/config")
async def config(request: Request):
    return {"enabled": bool(request.app.state.settings.oidc_issuer)}


@router.get("/login")
async def login(request: Request):
    settings = request.app.state.settings
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        try:
            meta = await discovery(client, settings)
        except (httpx.HTTPError, ValueError, KeyError):
            raise HTTPException(502, "无法读取OIDC认证中心配置") from None
    state, nonce, verifier, browser = [secrets.token_urlsafe(32) for _ in range(4)]
    async with request.app.state.db.sessions() as session, session.begin():
        await session.execute(
            delete(OAuthAttempt).where(OAuthAttempt.expires_at < datetime.now(timezone.utc))
        )
        session.add(
            OAuthAttempt(
                state_hash=sha(state),
                nonce=nonce,
                verifier=verifier,
                browser_hash=sha(browser),
                expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    response = RedirectResponse(
        meta["authorization_endpoint"]
        + ("&" if "?" in meta["authorization_endpoint"] else "?")
        + urlencode(
            {
                "response_type": "code",
                "client_id": settings.oidc_client_id,
                "redirect_uri": settings.oidc_redirect_uri,
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    response.set_cookie(
        "media_oidc",
        browser,
        max_age=300,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/api/v1/auth/sso",
    )
    return response


@router.get("/callback")
async def callback(request: Request, state: str = "", code: str = "", error: str = ""):
    if error or not code or not state or len(state) > 200 or len(code) > 4000:
        raise HTTPException(401, "SSO认证未完成")
    settings = request.app.state.settings
    async with request.app.state.db.sessions() as session, session.begin():
        attempt = await session.scalar(
            select(OAuthAttempt).where(OAuthAttempt.state_hash == sha(state)).with_for_update()
        )
        if (
            not attempt
            or attempt.expires_at < datetime.now(timezone.utc)
            or not secrets.compare_digest(
                attempt.browser_hash, sha(request.cookies.get("media_oidc", ""))
            )
        ):
            raise HTTPException(401, "SSO登录请求已过期或浏览器不匹配")
        nonce, verifier = attempt.nonce, attempt.verifier
        await session.delete(attempt)  # single-use even when the provider later rejects the code
    async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
        try:
            meta = await discovery(client, settings)
            body = {
                "grant_type": "authorization_code",
                "code": code,
                "client_id": settings.oidc_client_id,
                "redirect_uri": settings.oidc_redirect_uri,
                "code_verifier": verifier,
            }
            auth = None
            if settings.oidc_client_secret.get_secret_value():
                methods = meta.get("token_endpoint_auth_methods_supported", ["client_secret_basic"])
                if "client_secret_basic" in methods:
                    auth = (settings.oidc_client_id, settings.oidc_client_secret.get_secret_value())
                elif "client_secret_post" in methods:
                    body["client_secret"] = settings.oidc_client_secret.get_secret_value()
                else:
                    raise ValueError("unsupported client authentication")
            response = await client.post(meta["token_endpoint"], data=body, auth=auth)
            response.raise_for_status()
            token = response.json()["id_token"]
            header = jwt.get_unverified_header(token)
            if header.get("alg") not in {"RS256", "ES256"}:
                raise ValueError("unsupported algorithm")
            jwks = await client.get(meta["jwks_uri"])
            jwks.raise_for_status()
            keys = [
                k
                for k in jwks.json()["keys"]
                if k.get("kid") == header.get("kid") and k.get("use", "sig") == "sig"
            ]
            if len(keys) != 1 or keys[0].get("alg", header["alg"]) != header["alg"]:
                raise ValueError("invalid signing key")
            claims = jwt.decode(
                token,
                jwt.PyJWK.from_dict(keys[0]).key,
                algorithms=[header["alg"]],
                issuer=settings.oidc_issuer,
                audience=settings.oidc_client_id,
                options={"require": ["iss", "sub", "aud", "exp", "iat", "nonce"]},
            )
            if (
                not secrets.compare_digest(claims["nonce"], nonce)
                or not isinstance(claims["sub"], str)
                or not 1 <= len(claims["sub"]) <= 300
            ):
                raise ValueError("invalid identity")
            if (
                isinstance(claims["aud"], list) and len(claims["aud"]) > 1 or claims.get("azp")
            ) and claims.get("azp") != settings.oidc_client_id:
                raise ValueError("invalid authorized party")
        except (httpx.HTTPError, jwt.PyJWTError, ValueError, KeyError, TypeError):
            raise HTTPException(401, "SSO令牌验证失败，请重新登录") from None
    async with request.app.state.db.sessions() as session:
        identity = await session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.issuer == settings.oidc_issuer,
                ExternalIdentity.subject == claims["sub"],
            )
        )
        if identity:
            user = await session.get(User, identity.user_id)
        else:
            identity_digest = sha(settings.oidc_issuer + "\0" + claims["sub"])
            user = User(
                id=str(uuid4()),
                username="sso_" + identity_digest[:20],
                password_hash=await asyncio.to_thread(passwords.hash, secrets.token_urlsafe(48)),
                role="editor",
                token_version=0,
                disabled=False,
            )
            session.add(user)
            try:
                await session.flush()
                session.add(
                    ExternalIdentity(issuer=settings.oidc_issuer, subject=claims["sub"], user_id=user.id)
                )
                await session.commit()
            except IntegrityError:
                await session.rollback()
                identity = await session.scalar(
                    select(ExternalIdentity).where(
                        ExternalIdentity.issuer == settings.oidc_issuer,
                        ExternalIdentity.subject == claims["sub"],
                    )
                )
                if not identity:
                    raise HTTPException(409, "SSO身份正在创建，请重新登录") from None
                user = await session.get(User, identity.user_id)
        if not user or user.disabled:
            raise HTTPException(401, "账号已停用")
        access_token = issue_token(settings, user.id, user.token_version)
    response = RedirectResponse("/app/", status_code=303)
    response.delete_cookie("media_oidc", path="/api/v1/auth/sso")
    response.set_cookie(
        "media_session",
        access_token,
        max_age=settings.jwt_hours * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path="/api",
    )
    return response
