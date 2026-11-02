"""Optional stdio servers. Not started by the Demo web application.

python -m app.mcp_server --service llm|image|rag
RAG namespace must be bound explicitly with MEDIA_MCP_OWNER_ID.
"""

import argparse
import asyncio
import os
import sys
from contextlib import asynccontextmanager
from typing import Any, Literal
from uuid import uuid4

from mcp.server.fastmcp import FastMCP

from app.core.config import Settings
from app.core.database import Database
from app.core.llm_factory import LLM
from app.core.observability import configure_logging
from app.models import User
from app.schemas.content import Article, CardPack, TopicBatch
from app.services.image_engine import image_engine
from app.services.knowledge_service import KnowledgeService
from app.services.prompt_store import BUILTINS
from app.services.storage import Storage


def build_server(service, settings=None, owner_id=None):
    settings = settings or Settings()
    if service not in {"llm", "image", "rag"}:
        raise ValueError("Unknown service")
    if service == "rag" and not owner_id:
        raise ValueError("RAG服务器需要MEDIA_MCP_OWNER_ID，不能读取全部用户资料")
    llm = LLM(settings) if service == "llm" else None

    @asynccontextmanager
    async def lifespan(server):
        try:
            yield {}
        finally:
            if llm:
                await llm.close()

    server = FastMCP(
        f"Media-{service}",
        instructions="本地内容服务。真实模型模式会产生供应商用量，输入资料需要有权使用。",
        lifespan=lifespan,
    )

    if service == "llm":

        @server.tool(structured_output=True)
        async def generate_content(
            stage: Literal["TopicBatch", "Article", "CardPack"], context: dict
        ) -> dict[str, Any]:
            """按选题、长文、卡片Schema生成内容；默认使用本地项目的模型配置。"""
            schema = {"TopicBatch": TopicBatch, "Article": Article, "CardPack": CardPack}[stage]
            value = await llm.generate(schema, BUILTINS[stage], context, small=stage == "CardPack")
            return {"provider": settings.llm_provider, "content": value.model_dump(mode="json")}
    elif service == "image":

        @server.tool(structured_output=True)
        async def render_cards(pack: dict, theme: Literal["clean", "dark"] = "clean") -> dict[str, Any]:
            """将审核后的3–5页知识卡渲染为本地PNG；云生图模式按页产生用量。"""
            parsed = CardPack.model_validate(pack)
            storage = Storage(settings.storage_dir)
            task_id = "mcp-" + uuid4().hex
            files = await image_engine(settings, storage).render(
                task_id, 1, parsed.model_dump(mode="json"), theme, settings.llm_provider
            )
            return {
                "provider": settings.image_provider,
                "files": [
                    {"path": str(storage.resolve(f["path"])), "sha256": f["sha256"]} for f in files
                ],
            }
    else:

        @server.tool(structured_output=True)
        async def search_course(category: str, query: str) -> dict[str, Any]:
            """仅检索本机配置绑定账号的课程资料；不接受调用者指定其他用户。"""
            if not 1 <= len(query) <= 2000 or not 1 <= len(category) <= 80:
                raise ValueError("课程或问题长度不合法")
            db = Database(settings)
            try:
                async with db.sessions() as session:
                    user = await session.get(User, owner_id)
                    if not user or user.disabled:
                        raise ValueError("绑定账号不存在或已停用")
                return await KnowledgeService(db).search(owner_id, category, query)
            finally:
                await db.close()

    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--service", required=True, choices=["llm", "image", "rag"])
    args = parser.parse_args()
    settings = Settings()
    configure_logging(settings)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    build_server(args.service, settings, os.environ.get("MEDIA_MCP_OWNER_ID")).run(transport="stdio")


if __name__ == "__main__":
    main()
