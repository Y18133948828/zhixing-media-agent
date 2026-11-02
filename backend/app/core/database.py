from contextlib import asynccontextmanager

from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.core.config import Settings


class Database:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.engine = create_async_engine(
            settings.database_url.replace("postgresql://", "postgresql+psycopg://", 1),
            pool_pre_ping=True,
            pool_size=settings.database_business_pool,
            max_overflow=5,
            connect_args={"options": "-c timezone=UTC", "connect_timeout": 5},
        )
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def close(self):
        await self.engine.dispose()

    @asynccontextmanager
    async def checkpointer(self):
        if self.settings.checkpointer_backend == "memory":
            from langgraph.checkpoint.memory import InMemorySaver

            yield InMemorySaver()
            return
        # setup() is an explicit migration command, never run concurrently at every API startup.
        async with AsyncConnectionPool(
            self.settings.database_url,
            min_size=self.settings.database_pool_min,
            max_size=self.settings.database_pool_max,
            open=False,
            kwargs={
                "autocommit": True,
                "prepare_threshold": 0,
                "row_factory": dict_row,
                "connect_timeout": 5,
                "options": "-c search_path=langgraph -c timezone=UTC",
            },
        ) as pool:
            await pool.wait()
            yield AsyncPostgresSaver(pool)
