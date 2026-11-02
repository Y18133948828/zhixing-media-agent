import asyncio
import base64
import io
import json
from unittest.mock import patch

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from app.agents.mock_model import mock_output
from app.core.config import Settings
from app.core.llm_factory import LLM, ModelOutputError
from app.core.telemetry import runtime
from app.schemas.content import Article
from app.services.image_engine import APIImageRenderer
from app.services.storage import Storage


def settings(**overrides):
    return Settings(
        _env_file=None,
        ark_api_key="test-ark-private-key",
        ark_text_model="ep-test-text",
        ark_image_model="ep-test-image",
        **overrides,
    )


def test_ark_config_requires_credentials_and_valid_size_without_exposing_secrets():
    with pytest.raises(ValidationError, match="ARK_API_KEY"):
        Settings(_env_file=None, llm_provider="volcengine")
    with pytest.raises(ValidationError, match="ARK_IMAGE_MODEL") as error:
        Settings(_env_file=None, image_provider="volcengine", ark_api_key="private-not-for-logs")
    assert "private-not-for-logs" not in str(error.value)
    with pytest.raises(ValidationError, match="总像素"):
        settings(image_provider="volcengine", ark_image_size="1536x2048")
    with pytest.raises(ValidationError, match="3:4"):
        settings(image_provider="volcengine", ark_image_size="2048x2048")
    with pytest.raises(ValidationError, match="ARK_BASE_URL"):
        settings(
            llm_provider="volcengine", ark_base_url="https://ark.cn-beijing.volces.com/api/coding/v3"
        )
    assert settings(image_provider="volcengine").image_dimensions == [1728, 2304]


async def test_ark_text_uses_shared_key_thinking_and_upstream_sse_usage():
    article = mock_output("Article", {"selected_topic": {"title": "测试"}})
    raw = json.dumps(article, ensure_ascii=False)
    seen, metrics, events = [], [], []

    class Upstream(httpx.AsyncByteStream):
        async def __aiter__(self):
            for chunk in [raw[:500], raw[500:]]:
                yield (
                    "data: " + json.dumps({"choices": [{"delta": {"content": chunk}}]}) + "\n\n"
                ).encode()
            yield b'data: {"choices":[],"usage":{"prompt_tokens":70,"completion_tokens":90}}\n\ndata: [DONE]\n\n'

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/chat/completions"
        assert request.headers["Authorization"] == "Bearer test-ark-private-key"
        body = json.loads(request.content)
        assert body["model"] == "ep-test-text"
        assert body["thinking"] == {"type": "disabled"}
        assert body["response_format"] == {"type": "json_object"}
        assert body["stream_options"] == {"include_usage": True}
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Upstream())

    async def record(data):
        metrics.append(data)

    async def emit(kind, detail):
        events.append((kind, detail))

    token = runtime.set({"record_call": record, "emit": emit})
    llm = LLM(settings(llm_provider="volcengine"), httpx.MockTransport(handler))
    try:
        assert (await llm.generate(Article, "test", {})).title == "测试"
        assert len(seen) == 1 and metrics[0]["input_tokens"] == 70
        assert metrics[0]["output_tokens"] == 90
        assert "".join(event[1]["text"] for event in events) == article["markdown"]
    finally:
        runtime.reset(token)
        await llm.close()


async def test_ark_can_omit_thinking_for_older_models_and_use_small_model():
    def handler(request):
        body = json.loads(request.content)
        assert "thinking" not in body and "stream" not in body
        assert body["model"] == "ep-small"
        return httpx.Response(
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
                ]
            },
        )

    llm = LLM(
        settings(
            llm_provider="volcengine",
            ark_thinking="omit",
            llm_streaming=False,
            llm_small_model="ep-small",
        ),
        httpx.MockTransport(handler),
    )
    try:
        assert (await llm.generate(Article, "test", {}, small=True)).title == "测试"
    finally:
        await llm.close()


def png():
    buffer = io.BytesIO()
    Image.new("RGB", (6, 8), "green").save(buffer, format="PNG")
    return buffer.getvalue()


async def test_seedream_request_parameters_page_order_concurrency_and_png(tmp_path):
    active, peak, requests = 0, 0, []

    async def handler(request):
        nonlocal active, peak
        requests.append(request)
        active += 1
        peak = max(peak, active)
        body = json.loads(request.content)
        assert request.headers["Authorization"] == "Bearer test-ark-private-key"
        assert str(request.url) == "https://ark.cn-beijing.volces.com/api/v3/images/generations"
        assert body["model"] == "ep-test-image"
        assert body["size"] == "1728x2304" and body["watermark"] is True
        assert body["response_format"] == "b64_json" and body["stream"] is False
        assert "n" not in body and "sequential_image_generation" not in body
        await asyncio.sleep(0.02)
        active -= 1
        return httpx.Response(200, json={"data": [{"b64_json": base64.b64encode(png()).decode()}]})

    storage = Storage(tmp_path)
    renderer = APIImageRenderer(
        settings(image_provider="volcengine"), storage, httpx.MockTransport(handler)
    )
    pack = {"slides": [{"title": f"第{i}页"} for i in range(3)]}
    files = await renderer.render("sample", 1, pack, "clean", "live")
    assert len(files) == 3 and len(requests) == 3 and peak == 2
    assert [file["path"].rsplit("/", 1)[-1] for file in files] == [
        "card-01.png",
        "card-02.png",
        "card-03.png",
    ]
    assert all(Image.open(storage.resolve(file["path"])).size == (6, 8) for file in files)


@pytest.mark.parametrize(
    "data",
    [
        {"data": [{"error": {"code": "ContentRisk", "message": "private-provider-body"}}]},
        {"error": {"code": "RequestFailed", "message": "private-provider-body"}},
        {"data": []},
        {"data": [{"b64_json": "invalid"}]},
    ],
)
async def test_seedream_invalid_or_rejected_image_is_explicit_and_sanitized(tmp_path, data):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=data)

    renderer = APIImageRenderer(
        settings(image_provider="volcengine"), Storage(tmp_path), httpx.MockTransport(handler)
    )
    with pytest.raises(ModelOutputError) as error:
        await renderer.render("sample", 1, {"slides": [{"title": "技术卡片"}]}, "clean", "live")
    assert len(requests) == 1 and "private-provider-body" not in str(error.value)
    assert not list(tmp_path.rglob("*.png"))


async def test_seedream_url_response_is_downloaded_without_forwarding_key(tmp_path):
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "POST":
            assert json.loads(request.content)["response_format"] == "url"
            return httpx.Response(
                200, json={"data": [{"url": "https://images.example.test/result.png"}]}
            )
        assert "Authorization" not in request.headers
        return httpx.Response(200, content=png())

    renderer = APIImageRenderer(
        settings(image_provider="volcengine", ark_image_response_format="url"),
        Storage(tmp_path),
        httpx.MockTransport(handler),
    )
    with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("8.8.8.8", 443))]):
        files = await renderer.render("sample", 1, {"slides": [{"title": "技术卡片"}]}, "clean", "live")
    assert len(files) == 1 and [r.method for r in requests] == ["POST", "GET"]
