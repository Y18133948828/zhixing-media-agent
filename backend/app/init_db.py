import asyncio
import sys

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import BACKEND_ROOT, Settings
from app.core.database import Database


async def init_checkpoints():
    db = Database(Settings())
    try:
        async with db.engine.begin() as connection:
            await connection.execute(text("CREATE SCHEMA IF NOT EXISTS langgraph"))
        async with db.checkpointer() as checkpointer:
            if db.settings.checkpointer_backend == "postgres":
                await checkpointer.setup()
    finally:
        await db.close()


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    command.upgrade(Config(str(BACKEND_ROOT / "alembic.ini")), "head")
    asyncio.run(init_checkpoints())
    print("业务表和 LangGraph 断点表初始化完成。")


if __name__ == "__main__":
    main()
