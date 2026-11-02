import asyncio
import json
import time

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from app.agents.state import MediaState, PlanningOutput, VisualOutput, WritingOutput
from app.agents.tools.image_renderer.html_playwright import CardRenderer
from app.core.llm_factory import LLM
from app.core.observability import trace_span
from app.core.telemetry import emit, redact
from app.schemas.content import Article, CardPack, Review, SelectTopic, TopicBatch, TopicReview
from app.services.image_engine import image_engine
from app.services.prompt_store import prompt_for
from app.services.storage import Storage
from app.services.wechat_format import format_wechat, reference_sources


def build_graph(checkpointer, llm: LLM, storage: Storage, renderer: CardRenderer | None = None):
    renderer = renderer or image_engine(llm.settings, storage)

    async def topics(state):
        instruction, options = prompt_for(state, "TopicBatch")
        batch = await llm.generate(
            TopicBatch,
            instruction,
            {
                "task_input": state["task_input"],
                "topic_round": state.get("topic_round", 0),
                "topic_feedback": state.get("topic_feedback", ""),
                "previous_candidates": state.get("candidate_topics", []),
                **state.get("context", {}),
            },
            options=options,
        )
        if len(batch.topics) != state["task_input"]["topic_count"]:
            raise ValueError("模型返回的选题数量与要求不符")
        return {
            "candidate_topics": [t.model_dump(mode="json") for t in batch.topics],
            "selected_topic": {},
            "topic_review_index": 1,
            "topic_review_status": "pending",
        }

    def select_topic(state):
        # No I/O before interrupt: the whole node re-executes on resume in LangGraph 1.0.
        answer = SelectTopic.model_validate(
            interrupt({"type": "select_topic", "topics": state["candidate_topics"]})
        )
        topic = next(t for t in state["candidate_topics"] if t["id"] == answer.topic_id)
        return {"selected_topic": topic, "selection_record": answer.model_dump(mode="json")}

    def review_topic(state):
        index = state.get("topic_review_index", 1)
        steps = state["task_input"]["approval_steps"]
        answer = TopicReview.model_validate(
            interrupt(
                {
                    "type": "review_topic",
                    "topic": state["selected_topic"],
                    "topic_round": state.get("topic_round", 0),
                    "review_index": index,
                    "review_total": len(steps),
                    "reviewer": steps[index],
                }
            )
        )
        if answer.topic_round != state.get("topic_round", 0):
            raise ValueError("选题轮次已变化")
        approved = answer.decision == "approved"
        result = {
            "topic_review_status": "approved"
            if approved and index + 1 == len(steps)
            else "pending"
            if approved
            else "rejected",
            "topic_review_index": index + 1 if approved else 1,
            "topic_feedback": answer.feedback,
            "topic_review_history": [
                *state.get("topic_review_history", []),
                {**answer.model_dump(mode="json"), "reviewer": steps[index], "review_index": index},
            ],
        }
        if not approved:
            result["topic_round"] = state.get("topic_round", 0) + 1
        return result

    async def write_article(state):
        instruction, options = prompt_for(state, "Article")
        article = await llm.generate(
            Article,
            instruction,
            {
                "task_input": state["task_input"],
                "selected_topic": state["selected_topic"],
                "current_article": state.get("article_draft"),
                "human_feedback": state.get("human_feedback", ""),
                **state.get("context", {}),
            },
            options=options,
        )
        source_ids = {k["id"] for k in state.get("context", {}).get("knowledge", []) if k.get("id")}
        source_ids.update(
            s.get("id", f"input-{i + 1}") for i, s in enumerate(state["task_input"].get("sources", []))
        )
        if any(c.source_id not in source_ids for c in article.citations):
            raise ValueError("文章引用了不在任务资料中的来源")
        version = state.get("article_version", 0) + 1
        draft = article.model_dump(mode="json")
        return {
            "article_draft": draft,
            "article_version": version,
            "revision_count": version - 1,
            "review_status": "pending",
            "review_index": 0,
            "review_article_candidate": {},
            "approved_article": {},
            "revisions": [*state.get("revisions", []), {"version": version, "article": draft}],
        }

    def review_article(state):
        index = state.get("review_index", 0)
        steps = state["task_input"].get("approval_steps") or [{}]
        step = steps[index]
        answer = Review.model_validate(
            interrupt(
                {
                    "type": "review_article",
                    "article": state.get("review_article_candidate") or state["article_draft"],
                    "article_version": state["article_version"],
                    "review_index": index,
                    "reviewer": step,
                    "review_total": len(steps),
                }
            )
        )
        if answer.article_version != state["article_version"]:
            raise ValueError("审核文案版本已过期")
        result = {
            "review_status": answer.decision,
            "human_feedback": answer.feedback,
            "review_history": [
                *state.get("review_history", []),
                {**answer.model_dump(mode="json"), "review_index": index, "reviewer": step},
            ],
        }
        if answer.decision == "approved":
            article = (
                answer.edited_article.model_dump(mode="json")
                if answer.edited_article
                else state.get("review_article_candidate") or state["article_draft"]
            )
            result["review_article_candidate"] = article
            result["review_index"] = index + 1
            if index + 1 == len(steps):
                result["approved_article"] = article
            else:
                result["review_status"] = "pending"
        return result

    async def summarize(state):
        if state["task_input"].get("workflow_template") == "wechat_only":
            return {"xhs_pack": {}, "xhs_files": []}
        instruction, options = prompt_for(state, "CardPack")
        pack = await llm.generate(
            CardPack,
            instruction,
            {"task_input": state["task_input"], "approved_article": state["approved_article"]},
            small=True,
            options=options,
        )
        if len(pack.slides) != state["task_input"]["card_count"]:
            raise ValueError("模型返回的卡片页数与要求不符")
        return {"xhs_pack": pack.model_dump(mode="json")}

    async def render_images(state):
        if not state.get("xhs_pack"):
            return {"xhs_files": []}
        files = await renderer.render(
            state["task_id"],
            state["article_version"],
            state["xhs_pack"],
            state["task_input"]["theme"],
            state["mode"],
        )
        prefix = f"{state['task_id']}/v{state['article_version']}"
        files.append(
            storage.write(
                f"{prefix}/xiaohongshu.json",
                json.dumps(state["xhs_pack"], ensure_ascii=False, indent=2),
                "xhs_metadata",
                "application/json",
            )
        )
        return {"xhs_files": files}

    async def wechat_format(state):
        if state["task_input"].get("workflow_template") == "xhs_only":
            return {"wechat_html_content": "", "wechat_files": []}
        article = state["approved_article"]
        html = format_wechat(article, reference_sources(state))
        prefix = f"{state['task_id']}/v{state['article_version']}"
        files = [
            storage.write(
                f"{prefix}/wechat.html",
                '<!doctype html><meta charset="utf-8">' + html,
                "wechat_html",
                "text/html",
            ),
            storage.write(
                f"{prefix}/article.md",
                f"# {article['title']}\n\n{article['markdown']}",
                "article_markdown",
                "text/markdown",
            ),
        ]
        return {"wechat_html_content": html, "wechat_files": files}

    async def measured(node, name, state):
        tick = time.monotonic()
        await emit("node_started", {"node": name})
        try:
            trace_input = {
                "task_id": state["task_id"],
                "article_version": state.get("article_version", 0),
                "state_keys": list(state),
                "prompt_version": state["task_input"].get("prompt_version"),
            }
            if llm.settings.tracing_include_content:
                trace_input["state"] = json.loads(redact(json.dumps(state, ensure_ascii=False)))
            async with trace_span(name, llm.settings, trace_input):
                async with asyncio.timeout(llm.settings.node_timeouts.get(name, 300)):
                    result = await node(state)
        except Exception:
            await emit(
                "node_failed", {"node": name, "duration_ms": round((time.monotonic() - tick) * 1000, 2)}
            )
            raise
        await emit(
            "node_completed", {"node": name, "duration_ms": round((time.monotonic() - tick) * 1000, 2)}
        )
        return result

    # The planning subgraph is reusable. The parent owns persistence and both human gates.
    async def run_planning(state):
        return await measured(topics, "topics", state)

    planning = StateGraph(MediaState, output_schema=PlanningOutput)
    planning.add_node("plan_topics", run_planning)
    planning.add_edge(START, "plan_topics")
    planning.add_edge("plan_topics", END)

    async def run_writing(state):
        return await measured(write_article, "write_article", state)

    writing = StateGraph(MediaState, output_schema=WritingOutput)
    writing.add_node("draft", run_writing)
    writing.add_edge(START, "draft")
    writing.add_edge("draft", END)

    async def run_summary(state):
        return await measured(summarize, "summarize", state)

    async def run_render(state):
        return await measured(render_images, "render_images", state)

    visuals = StateGraph(MediaState, output_schema=VisualOutput)
    visuals.add_node("outline", run_summary)
    visuals.add_node("render", run_render)
    visuals.add_edge(START, "outline")
    visuals.add_edge("outline", "render")
    visuals.add_edge("render", END)
    graph = StateGraph(MediaState)
    for name, node in [
        ("topics", planning.compile()),
        ("select_topic", select_topic),
        ("review_topic", review_topic),
        ("write_article", writing.compile()),
        ("review_article", review_article),
        ("summarize", summarize),
        ("render_images", render_images),
        ("wechat_format", wechat_format),
        ("visual_production", visuals.compile()),
        ("ready", lambda state: {"ready": True}),
    ]:
        if name in {"summarize", "render_images", "wechat_format"}:

            async def wrapped(state, selected=node, label=name):
                return await measured(selected, label, state)

            graph.add_node(name, wrapped)
        else:
            graph.add_node(name, node)
    graph.add_edge(START, "topics")
    graph.add_edge("topics", "select_topic")
    graph.add_conditional_edges(
        "select_topic",
        lambda state: (
            "review_topic"
            if state["task_input"].get("review_topics")
            and len(state["task_input"].get("approval_steps", [])) > 1
            else "write_article"
        ),
        ["review_topic", "write_article"],
    )
    graph.add_conditional_edges(
        "review_topic",
        lambda state: (
            "write_article"
            if state["topic_review_status"] == "approved"
            else "review_topic"
            if state["topic_review_status"] == "pending"
            else "topics"
        ),
        ["write_article", "review_topic", "topics"],
    )
    graph.add_edge("write_article", "review_article")
    graph.add_conditional_edges(
        "review_article",
        lambda state: (
            ["visual_production", "wechat_format"]
            if state["review_status"] == "approved"
            else ["review_article"]
            if state["review_status"] == "pending"
            else ["write_article"]
        ),
        ["visual_production", "wechat_format", "write_article", "review_article"],
    )
    graph.add_edge("summarize", "render_images")
    graph.add_edge(["render_images", "wechat_format"], "ready")
    graph.add_edge(["visual_production", "wechat_format"], "ready")
    graph.add_edge("ready", END)
    return graph.compile(checkpointer=checkpointer)
