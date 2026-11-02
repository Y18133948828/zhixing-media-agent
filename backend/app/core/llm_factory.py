import asyncio
import hashlib
import json
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

from app.core.config import Settings
from app.core.content_safety import check_content, secrets_filter
from app.core.resilience import CircuitBreaker, CircuitOpen
from app.core.telemetry import emit, partial_json_string, record_call, redact, runtime

T = TypeVar("T", bound=BaseModel)


class ModelOutputError(RuntimeError):
    pass


class RetryableModelError(ModelOutputError):
    pass


class TerminalModelError(ModelOutputError):
    """Configuration errors or provider refusals must not trigger another provider."""


class LLM:
    """Compatible HTTP adapter with upstream SSE, usage and strict final validation."""

    def __init__(self, settings: Settings, transport=None):
        self.settings = settings
        self.transport = transport
        self.client = httpx.AsyncClient(timeout=settings.llm_timeout_seconds, transport=transport)
        self.failures = 0
        self.open_until = 0.0
        self.breakers = {}
        self.cache = {}
        self.latencies, self.recent_costs = {}, {}

    async def close(self):
        await self.client.aclose()

    def route(self, stage, context, small, options):
        primary = (self.settings.llm_small_model if small else "") or self.settings.text_model
        if options.get("model_locked"):
            return options.get("model") or primary, "explicit"
        primary = options.get("model") or primary
        policy = options.get("routing_strategy", self.settings.routing_strategy)
        if (
            policy == "adaptive"
            and self.settings.llm_small_model
            and stage in {"TopicBatch", "CardPack"}
        ):
            size = len(json.dumps(context, ensure_ascii=False))
            slow = self.latencies.get(primary, 0) > options.get(
                "routing_latency_budget_seconds", self.settings.routing_latency_budget_seconds
            )
            budget = options.get("routing_cost_budget", self.settings.routing_cost_budget)
            costly = budget is not None and self.recent_costs.get(primary, 0) > budget
            if size < 6000 or size < 16000 and (slow or costly):
                return self.settings.llm_small_model, "adaptive_small"
        return primary, "stage"

    async def generate(
        self, schema: type[T], instruction: str, context: dict, *, small=False, options=None
    ) -> T:
        context = dict(context)
        if isinstance(context.get("task_input"), dict):
            context["task_input"] = {
                k: v
                for k, v in context["task_input"].items()
                if k not in {"prompt_snapshots", "approval_steps", "prompt_ids", "reviewer_ids"}
            }
        options = dict(options or {})
        model, reason = self.route(schema.__name__, context, small, options)
        options.update(model=model, route_reason=reason)
        payload = json.dumps(context, ensure_ascii=False)
        await check_content(self.settings, secrets_filter(redact(payload)), schema.__name__ + ":input")
        scope = (runtime.get() or {}).get("owner_id")
        key = hashlib.sha256(
            json.dumps(
                [scope, self.settings.llm_provider, instruction, context, options],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        cached = self.cache.get(key)
        if (
            schema.__name__ != "TopicBatch"
            and self.settings.llm_provider != "mock"
            and cached
            and cached[0] > time.monotonic()
        ):
            await record_call(
                stage=schema.__name__,
                model=cached[2],
                duration_ms=0,
                input_tokens=None,
                output_tokens=None,
                cost=0,
                success=True,
                usage_source="cache",
                details={"prompt_digest": options.get("prompt_digest")},
            )
            value = schema.model_validate(cached[1])
            if schema.__name__ == "Article":
                await emit("article_delta", {"text": value.markdown, "reset": True, "source": "cache"})
            return value
        try:
            value = await self._generate(schema, instruction, context, small=small, options=options)
            result_model = model
        except TerminalModelError:
            raise
        except ModelOutputError:
            if self.settings.llm_fallback_provider == "disabled":
                raise
            fallback = self.settings.model_copy(
                update={
                    "llm_provider": self.settings.llm_fallback_provider,
                    "llm_base_url": self.settings.llm_fallback_base_url or self.settings.llm_base_url,
                    "llm_api_key": self.settings.llm_fallback_api_key,
                    "llm_model": self.settings.llm_fallback_model,
                    "ark_text_model": self.settings.llm_fallback_model,
                    "llm_fallback_provider": "disabled",
                }
            )
            await emit(
                "model_fallback",
                {
                    "stage": schema.__name__,
                    "provider": fallback.llm_provider,
                    "model": fallback.text_model,
                },
            )
            alternate = LLM(fallback, self.transport)
            result_model = fallback.text_model
            try:
                value = await alternate.generate(
                    schema,
                    instruction,
                    context,
                    options={**options, "model": fallback.text_model, "model_locked": True},
                )
            finally:
                await alternate.close()
        await check_content(
            self.settings, secrets_filter(redact(value.model_dump_json())), schema.__name__ + ":output"
        )
        if (
            self.settings.llm_cache_seconds
            and schema.__name__ != "TopicBatch"
            and self.settings.llm_provider != "mock"
        ):
            self.cache[key] = (
                time.monotonic() + self.settings.llm_cache_seconds,
                value.model_dump(mode="json"),
                result_model,
            )
            while len(self.cache) > 128:
                self.cache.pop(next(iter(self.cache)))
        return value

    async def _generate(
        self, schema: type[T], instruction: str, context: dict, *, small=False, options=None
    ) -> T:
        options = options or {}
        started = time.monotonic()
        if self.settings.llm_provider == "mock":
            from app.agents.mock_model import mock_output

            value = schema.model_validate(mock_output(schema.__name__, context))
            if schema.__name__ == "Article":
                for offset in range(0, len(value.markdown), 120):
                    await emit(
                        "article_delta",
                        {
                            "text": value.markdown[offset : offset + 120],
                            "reset": offset == 0,
                            "source": "mock",
                        },
                    )
                    await asyncio.sleep(0.01)
            await record_call(
                stage=schema.__name__,
                model="mock",
                duration_ms=(time.monotonic() - started) * 1000,
                input_tokens=None,
                output_tokens=None,
                cost=None,
                success=True,
                usage_source="mock",
            )
            return value
        model = options.get("model") or (
            (self.settings.llm_small_model if small else "") or self.settings.text_model
        )
        breaker = self.breakers.setdefault(model, CircuitBreaker())
        try:
            await breaker.acquire()
        except CircuitOpen as exc:
            raise ModelOutputError(str(exc)) from None
        system = instruction + "\n只输出符合以下 JSON Schema 的 JSON 对象，不加代码围栏：\n"
        system += json.dumps(schema.model_json_schema(), ensure_ascii=False)
        user = json.dumps(context, ensure_ascii=False)
        if self.settings.redact_pii:
            user = secrets_filter(redact(user))
        error = "模型请求失败"
        for attempt in range(self.settings.llm_attempts):
            tick = time.monotonic()
            data, success = {}, False
            try:
                raw, data = await self._request(model, system, user, schema.__name__, options)
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()
                value = schema.model_validate_json(
                    secrets_filter(redact(raw)) if self.settings.redact_pii else raw
                )
                success = True
                self.failures = 0
                await breaker.success()
                return value
            except asyncio.CancelledError:
                await breaker.cancelled()
                raise
            except (ValidationError, ValueError, KeyError, IndexError, TypeError, AttributeError):
                error = "模型内容未通过结构校验，请调整模型或提示词"
                user += "\n请重新检查 JSON 字段、长度和页数，输出完整正确对象。"
            except (httpx.TransportError, RetryableModelError):
                error = "模型暂时不可用或连接超时，请稍后重试"
            except TerminalModelError:
                await breaker.failure()
                raise
            except ModelOutputError as exc:
                error = str(exc)
                break
            finally:
                usage = data.get("usage") or {}
                if not isinstance(usage, dict):
                    usage = {}
                inputs = usage.get("prompt_tokens", usage.get("input_tokens"))
                outputs = usage.get("completion_tokens", usage.get("output_tokens"))
                # An incompatible provider's malformed usage must not invalidate good content.
                inputs = inputs if type(inputs) is int and 0 <= inputs <= 2147483647 else None
                outputs = outputs if type(outputs) is int and 0 <= outputs <= 2147483647 else None
                cost = None
                if (
                    inputs is not None
                    and outputs is not None
                    and self.settings.llm_input_price_per_million is not None
                    and self.settings.llm_output_price_per_million is not None
                ):
                    cost = (
                        inputs * self.settings.llm_input_price_per_million
                        + outputs * self.settings.llm_output_price_per_million
                    ) / 1_000_000
                await record_call(
                    stage=schema.__name__,
                    model=model,
                    duration_ms=(time.monotonic() - tick) * 1000,
                    input_tokens=inputs,
                    output_tokens=outputs,
                    cost=cost,
                    success=success,
                    usage_source="provider" if inputs is not None else "unavailable",
                    details={
                        "prompt_digest": options.get("prompt_digest"),
                        "route_reason": options.get("route_reason"),
                        "provider": self.settings.llm_provider,
                        "currency": self.settings.cost_currency,
                        "temperature": options.get(
                            "temperature", self.settings.stage_temperatures.get(schema.__name__)
                        ),
                        "max_tokens": options.get(
                            "max_tokens",
                            self.settings.stage_max_tokens.get(
                                schema.__name__, self.settings.llm_max_tokens
                            ),
                        ),
                    },
                )
                self.latencies[model] = (
                    self.latencies.get(model, time.monotonic() - tick) + time.monotonic() - tick
                ) / 2
                if cost is not None:
                    self.recent_costs[model] = cost
            if attempt < self.settings.llm_attempts - 1:
                await asyncio.sleep(2**attempt)
        await breaker.failure()
        raise ModelOutputError(error)

    async def _request(self, model, system, user, stage, options=None):
        options = options or {}
        maximum = options.get("max_tokens") or min(
            self.settings.stage_max_tokens.get(stage, self.settings.llm_max_tokens),
            self.settings.llm_max_tokens,
        )
        temperature = options.get("temperature", self.settings.stage_temperatures.get(stage))
        if self.settings.llm_provider == "anthropic":
            body = {
                "model": model,
                "max_tokens": maximum,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            }
            if temperature is not None:
                body["temperature"] = temperature
            response = await self.client.post(
                self.settings.llm_base_url.rstrip("/") + "/messages",
                headers={
                    "x-api-key": self.settings.llm_api_key.get_secret_value(),
                    "anthropic-version": "2023-06-01",
                },
                json=body,
            )
            self._check_status(response.status_code)
            data = response.json()
            if data.get("error") or data.get("stop_reason") == "refusal":
                raise TerminalModelError("模型服务拒绝了请求，请检查配置或修改内容")
            return "".join(x.get("text", "") for x in data["content"] if x["type"] == "text"), data
        body = {
            "model": model,
            "max_tokens": maximum,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        }
        if temperature is not None:
            body["temperature"] = temperature
        if self.settings.llm_json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.settings.llm_provider == "volcengine" and self.settings.ark_thinking != "omit":
            body["thinking"] = {"type": self.settings.ark_thinking}
        if self.settings.llm_streaming:
            body.update(stream=True, stream_options={"include_usage": True})
        url = self.settings.text_base_url.rstrip("/") + "/chat/completions"
        headers = {"Authorization": "Bearer " + self.settings.text_api_key.get_secret_value()}
        async with self.client.stream("POST", url, headers=headers, json=body) as response:
            self._check_status(response.status_code)
            if "text/event-stream" not in response.headers.get("content-type", ""):
                await response.aread()
                data = response.json()
                if data.get("error") or any(
                    c.get("finish_reason") == "content_filter" or c.get("message", {}).get("refusal")
                    for c in data.get("choices", [])
                ):
                    raise TerminalModelError("模型服务拒绝了请求，请检查配置或修改内容")
                return data["choices"][0]["message"]["content"], data
            raw, sent, last, data = "", "", time.monotonic(), {}
            async for line in response.aiter_lines():
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                chunk = json.loads(payload)
                if chunk.get("error") or any(
                    c.get("finish_reason") == "content_filter" or c.get("delta", {}).get("refusal")
                    for c in chunk.get("choices", [])
                ):
                    raise TerminalModelError("模型服务拒绝了请求，请检查配置或修改内容")
                if chunk.get("usage"):
                    data["usage"] = chunk["usage"]
                for choice in chunk.get("choices", []):
                    raw += choice.get("delta", {}).get("content") or ""
                if stage == "Article" and time.monotonic() - last > 0.08:
                    text = partial_json_string(raw, "markdown")
                    if text.startswith(sent) and len(text) > len(sent):
                        await emit(
                            "article_delta",
                            {"text": text[len(sent) :], "reset": not sent, "source": "provider"},
                        )
                        sent, last = text, time.monotonic()
            if stage == "Article":
                text = partial_json_string(raw, "markdown")
                if text.startswith(sent) and len(text) > len(sent):
                    await emit(
                        "article_delta",
                        {"text": text[len(sent) :], "reset": not sent, "source": "provider"},
                    )
            return raw, data

    @staticmethod
    def _check_status(status):
        if status == 429 or status >= 500:
            raise RetryableModelError("模型服务暂时不可用")
        if status >= 400:
            raise TerminalModelError(f"模型接口返回 HTTP {status}；请检查配置或供应商服务")
