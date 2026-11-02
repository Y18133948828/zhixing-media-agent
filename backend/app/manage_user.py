"""Local administrator bootstrap; never grants admin on public registration."""

import argparse
import asyncio
import sys

from sqlalchemy import select

from app.core.config import Settings
from app.core.database import Database
from app.models import User


async def update_role(username, role):
    db = Database(Settings())
    try:
        async with db.sessions() as session, session.begin():
            user = await session.scalar(select(User).where(User.username == username).with_for_update())
            if not user:
                raise SystemExit("账号不存在，请先在工作台注册")
            user.role = role
            user.token_version += 1
            print(f"已将 {username} 设置为 {role}；请重新登录。")
    finally:
        await db.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--username", required=True)
    parser.add_argument("--role", required=True, choices=["editor", "manager", "director", "admin"])
    args = parser.parse_args()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(update_role(args.username, args.role))


if __name__ == "__main__":
    main()
