from uuid import uuid4

import pytest
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agents.graph import build_graph
from app.core.config import Settings
from app.core.llm_factory import LLM
from app.schemas.content import TaskCreate
from app.services.storage import Storage


class FakeRenderer:
    calls = 0

    async def render(self, *args):
        self.calls += 1
        return []


@pytest.mark.parametrize("count", [3, 4, 5])
async def test_graph_review_loop_manual_edit_and_join(tmp_path, count):
    llm = LLM(Settings(_env_file=None))
    renderer = FakeRenderer()
    graph = build_graph(InMemorySaver(), llm, Storage(tmp_path), renderer)
    config = {"configurable": {"thread_id": str(uuid4())}}
    try:
        result = await graph.ainvoke(
            {
                "task_id": str(uuid4()),
                "task_input": TaskCreate(card_count=count).model_dump(mode="json"),
                "mode": "mock",
            },
            config,
        )
        assert result["__interrupt__"][0].value["type"] == "select_topic"
        assert renderer.calls == 0
        select = {
            "interrupt_id": result["__interrupt__"][0].id,
            "topic_id": "topic-1",
            "operator": "测试",
        }
        result = await graph.ainvoke(Command(resume=select), config)
        assert result["article_version"] == 1 and renderer.calls == 0
        reject = {
            "interrupt_id": result["__interrupt__"][0].id,
            "article_version": 1,
            "decision": "rejected",
            "feedback": "开头更通俗",
            "operator": "测试",
        }
        result = await graph.ainvoke(Command(resume=reject), config)
        assert result["article_version"] == 2
        assert "开头更通俗" in result["article_draft"]["markdown"]
        assert result["review_history"][0]["decision"] == "rejected"
        edited = {**result["article_draft"], "title": "人工定稿标题"}
        approve = {
            "interrupt_id": result["__interrupt__"][0].id,
            "article_version": 2,
            "decision": "approved",
            "operator": "测试",
            "edited_article": edited,
        }
        result = await graph.ainvoke(Command(resume=approve), config)
        assert result["ready"] and renderer.calls == 1
        assert len(result["xhs_pack"]["slides"]) == count
        assert "人工定稿标题" in result["wechat_html_content"]
        assert result["approved_article"]["title"] == "人工定稿标题"
        assert result["revisions"][1]["article"]["title"] != "人工定稿标题"
        assert not (await graph.aget_state(config)).next
    finally:
        await llm.close()
