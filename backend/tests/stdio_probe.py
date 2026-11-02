"""MCP client process uses Proactor on Windows; main app tests use Selector for PostgreSQL."""

import asyncio
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main():
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "app.mcp_server", "--service", "llm"],
        cwd=str(Path(__file__).resolve().parents[1]),
        env={"LLM_PROVIDER": "mock", "IMAGE_PROVIDER": "template", "PYTHONUTF8": "1"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert [t.name for t in tools.tools] == ["generate_content"]
            result = await session.call_tool(
                "generate_content",
                {"stage": "TopicBatch", "context": {"task_input": {"topic_count": 3}}},
            )
            assert not result.isError
            assert result.structuredContent["provider"] == "mock"
            assert len(result.structuredContent["content"]["topics"]) == 3
    print("stdio integration OK")


if __name__ == "__main__":
    asyncio.run(main())
