import json

import httpx
import pytest

from app.agents.mock_model import mock_output
from app.core.config import Settings
from app.core.llm_factory import LLM, ModelOutputError
from app.schemas.content import TaskCreate, TopicBatch


async def test_compatible_api_custom_base_and_json_repair():
    seen = []

    def handler(request):
        seen.append(request)
        assert str(request.url) == "https://provider.test/custom/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer fake-secret"
        assert "response_format" not in json.loads(request.content)
        payload = (
            "invalid"
            if len(seen) == 1
            else json.dumps(mock_output("TopicBatch", {"task_input": TaskCreate().model_dump()}))
        )
        return httpx.Response(200, json={"choices": [{"message": {"content": payload}}]})

    llm = LLM(
        Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="fake-secret",
            llm_base_url="https://provider.test/custom/v1/",
            llm_json_mode=False,
        ),
        httpx.MockTransport(handler),
    )
    try:
        output = await llm.generate(TopicBatch, "生成选题", {"task_input": {}})
        assert len(output.topics) == 4 and len(seen) == 2
    finally:
        await llm.close()


async def test_provider_errors_do_not_expose_secrets():
    llm = LLM(
        Settings(_env_file=None, llm_provider="openai_compatible", llm_api_key="private-key"),
        httpx.MockTransport(lambda request: httpx.Response(401, text="private-key error details")),
    )
    try:
        with pytest.raises(ModelOutputError) as error:
            await llm.generate(TopicBatch, "", {})
        assert "401" in str(error.value) and "private-key" not in str(error.value)
    finally:
        await llm.close()
