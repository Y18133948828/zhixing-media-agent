import asyncio
import base64
import io
import json

import httpx
import pytest
from PIL import Image

from app.agents.mock_model import mock_output
from app.core.config import Settings
from app.core.llm_factory import LLM, ModelOutputError
from app.core.telemetry import partial_json_string, runtime
from app.schemas.content import Article, TaskCreate
from app.services.image_engine import APIImageRenderer
from app.services.retrieval import retrieve
from app.services.storage import Storage


def test_partial_json_preserves_escapes_and_surrogate_pairs():
    assert partial_json_string('{"markdown":"中文\\n代码\\uD83D', "markdown") == "中文\n代码"
    assert partial_json_string('{"markdown":"中文\\n代码\\uD83D\\uDE80"}', "markdown") == "中文\n代码🚀"


async def test_real_sse_adapter_collects_usage_and_validates_final_article():
    article = mock_output("Article", {"selected_topic": {"title": "测试"}})
    raw = json.dumps(article, ensure_ascii=False)
    metrics, events = [], []

    class Upstream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for index in range(0, len(raw), 200):
                yield (
                    "data: "
                    + json.dumps(
                        {"choices": [{"delta": {"content": raw[index : index + 200]}}]},
                        ensure_ascii=False,
                    )
                    + "\n\n"
                ).encode()
                await asyncio.sleep(0.03)
            yield b'data: {"usage":{"prompt_tokens":100,"completion_tokens":200},"choices":[]}\n\ndata: [DONE]\n\n'

    async def on_event(kind, detail):
        events.append((kind, detail))

    async def on_metric(data):
        metrics.append(data)

    ctx = runtime.set({"emit": on_event, "record_call": on_metric})
    llm = LLM(
        Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="fake-key",
            llm_input_price_per_million=1,
            llm_output_price_per_million=2,
        ),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Upstream()
            )
        ),
    )
    try:
        result = await llm.generate(Article, "test", {})
        assert result.markdown == article["markdown"].strip()
        assert (
            "".join(detail["text"] for kind, detail in events if kind == "article_delta")
            == article["markdown"]
        )
        assert len(events) > 1
        assert metrics[0]["input_tokens"] == 100 and metrics[0]["cost"] == pytest.approx(0.0005)
    finally:
        runtime.reset(ctx)
        await llm.close()


async def test_gemini_image_adapter_keeps_page_order_and_ratio(tmp_path):
    image = Image.new("RGB", (3, 4), "green")
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode()

    async def handler(request):
        assert request.headers["x-goog-api-key"] == "fake"
        assert "Authorization" not in request.headers
        await asyncio.sleep(0.01)
        return httpx.Response(
            200, json={"candidates": [{"content": {"parts": [{"inlineData": {"data": encoded}}]}}]}
        )

    settings = Settings(
        _env_file=None,
        image_provider="gemini",
        image_api_key="fake",
        image_model="test-model",
        image_base_url="https://images.test",
        image_auth="x-goog-api-key",
    )
    renderer = APIImageRenderer(settings, Storage(tmp_path), httpx.MockTransport(handler))
    pack = mock_output("CardPack", {"task_input": TaskCreate(card_count=3).model_dump()})
    files = await renderer.render("sample", 1, pack, "clean", "mock")
    assert [f["path"].rsplit("/", 1)[-1] for f in files] == ["card-01.png", "card-02.png", "card-03.png"]


async def test_image_policy_rejection_does_not_replace_subject(tmp_path):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(400, json={"error": "policy rejection"})

    settings = Settings(
        _env_file=None,
        image_provider="gemini",
        image_api_key="fake",
        image_model="test-model",
        image_base_url="https://images.test",
    )
    renderer = APIImageRenderer(settings, Storage(tmp_path), httpx.MockTransport(handler))
    with pytest.raises(ModelOutputError):
        await renderer.render("sample", 1, {"slides": [{"title": "技术卡片"}]}, "clean", "mock")
    assert len(calls) == 1


def test_course_retrieval_selects_relevant_documents():
    from types import SimpleNamespace

    docs = [
        SimpleNamespace(title="Python异步", body="Python async await工作流"),
        SimpleNamespace(title="其他", body="今天的天气很好"),
    ]
    assert retrieve(docs, "Python工作流") == [docs[0]]


async def test_malformed_usage_is_unknown_and_does_not_break_valid_content():
    metrics = []

    async def save(data):
        metrics.append(data)

    ctx = runtime.set({"record_call": save})
    llm = LLM(
        Settings(
            _env_file=None, llm_provider="openai_compatible", llm_api_key="fake", llm_streaming=False
        ),
        httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                json={
                    "choices": [
                        {
                            "message": {
                                "content": json.dumps(
                                    mock_output("Article", {"selected_topic": {"title": "测试"}})
                                )
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": "100", "completion_tokens": -1},
                },
            )
        ),
    )
    try:
        assert (await llm.generate(Article, "test", {})).title
        assert metrics[0]["input_tokens"] is None and metrics[0]["output_tokens"] is None
        assert metrics[0]["cost"] is None
    finally:
        runtime.reset(ctx)
        await llm.close()
