import asyncio

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.security import DUMMY_PASSWORD_HASH, auth, issue_token, passwords, verify_password
from app.models import User
from app.schemas.content import StrictModel

router = APIRouter(prefix="/api/v1/auth", tags=["登录"])


class Credentials(StrictModel):
    username: str = Field(min_length=3, max_length=50, pattern=r"^[\w\u4e00-\u9fff.-]+$")
    password: str = Field(min_length=8, max_length=100)


def token_result(settings, user):
    return {
        "access_token": issue_token(settings, user.id, user.token_version),
        "token_type": "bearer",
        "user": {"id": user.id, "username": user.username, "role": user.role},
    }


@router.post("/register", status_code=201)
async def register(body: Credentials, request: Request):
    encoded = await asyncio.to_thread(passwords.hash, body.password)
    async with request.app.state.db.sessions() as session:
        user = User(username=body.username, password_hash=encoded)
        session.add(user)
        try:
            await session.commit()
        except IntegrityError:
            raise HTTPException(409, "用户名已存在") from None
        return token_result(request.app.state.settings, user)


@router.post("/login")
async def login(body: Credentials, request: Request):
    async with request.app.state.db.sessions() as session:
        user = await session.scalar(select(User).where(User.username == body.username))
    # Hash verification runs for unknown names too; errors don't reveal account existence.
    encoded = user.password_hash if user else DUMMY_PASSWORD_HASH
    valid = await verify_password(body.password, encoded)
    if user is None or user.disabled or not valid:
        raise HTTPException(401, "用户名或密码错误")
    return token_result(request.app.state.settings, user)


@router.get("/me", dependencies=[Depends(auth)])
async def me(request: Request):
    return {"id": request.state.owner_id, "username": request.state.username, "role": request.state.role}


@router.post("/logout", dependencies=[Depends(auth)])
async def logout(request: Request, response: Response):
    if request.state.owner_id:
        async with request.app.state.db.sessions() as session, session.begin():
            user = await session.get(User, request.state.owner_id)
            user.token_version += 1
    response.delete_cookie("media_session", path="/api")
    return {"logged_out": True, "scope": "all_sessions"}


class PasswordChange(StrictModel):
    old_password: str = Field(min_length=8, max_length=100)
    new_password: str = Field(min_length=8, max_length=100)


@router.post("/password", dependencies=[Depends(auth)])
async def change_password(body: PasswordChange, request: Request, response: Response):
    from app.core.security import require_account

    owner = require_account(request)
    async with request.app.state.db.sessions() as session, session.begin():
        user = await session.scalar(select(User).where(User.id == owner).with_for_update())
        if not await verify_password(body.old_password, user.password_hash):
            raise HTTPException(401, "原密码不正确")
        user.password_hash = await asyncio.to_thread(passwords.hash, body.new_password)
        user.token_version += 1
        result = token_result(request.app.state.settings, user)
        response.delete_cookie("media_session", path="/api")
        return result
