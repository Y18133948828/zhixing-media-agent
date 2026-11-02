import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import Depends, HTTPException, Request
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

from app.models import User

passwords = PasswordHasher()
DUMMY_PASSWORD_HASH = passwords.hash("not-an-account-password")
bearer = HTTPBearer(auto_error=False)
api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def jwt_key(settings):
    return (
        settings.jwt_secret.get_secret_value()
        or hashlib.sha256(("media-jwt-v1:" + settings.api_key.get_secret_value()).encode()).hexdigest()
    )


def issue_token(settings, user_id, token_version=0):
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": user_id,
            "ver": token_version,
            "iat": now,
            "exp": now + timedelta(hours=settings.jwt_hours),
            "iss": "media-demo",
            "aud": "media-workbench",
        },
        jwt_key(settings),
        algorithm="HS256",
    )


async def verify_password(plain, encoded):
    try:
        return await asyncio.to_thread(passwords.verify, encoded, plain)
    except (VerificationError, InvalidHashError):
        return False


async def auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    key: str | None = Depends(api_key_header),
):
    settings = request.app.state.settings
    raw_token = credentials.credentials if credentials else request.cookies.get("media_session")
    if raw_token:
        try:
            claims = jwt.decode(
                raw_token,
                jwt_key(settings),
                algorithms=["HS256"],
                audience="media-workbench",
                issuer="media-demo",
                options={"require": ["exp", "iat", "sub"]},
            )
            async with request.app.state.db.sessions() as session:
                user = await session.get(User, claims["sub"])
            if user is None or user.disabled or claims.get("ver", 0) != user.token_version:
                raise ValueError("user missing")
            request.state.owner_id = user.id
            request.state.username = user.username
            request.state.role = user.role
            return
        except (jwt.PyJWTError, ValueError, KeyError):
            raise HTTPException(401, "登录已过期，请重新登录") from None
    if key and secrets.compare_digest(key, settings.api_key.get_secret_value()):
        request.state.owner_id = None
        request.state.username = "本地调试"
        request.state.role = "debug"
        return
    raise HTTPException(401, "请先登录，或提供正确的 X-API-Key")


def require_account(request):
    if not request.state.owner_id:
        raise HTTPException(403, "此功能需要登录个人账号")
    return request.state.owner_id


def require_admin(request):
    require_account(request)
    if request.state.role != "admin":
        raise HTTPException(403, "此操作需要管理员权限")
