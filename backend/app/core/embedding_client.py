import asyncio
import math
import time

import httpx
from fastapi import HTTPException

from app.core.telemetry import record_call, redact


def valid_vector(value):
    if (
        not isinstance(value, list)
        or not 1 <= len(value) <= 8192
        or any(type(v) not in (int, float) or not math.isfinite(v) for v in value)
    ):
        raise ValueError("invalid vector")
    return value


class Embedder:
    def __init__(self, settings, transport=None):
        self.settings, self.transport = settings, transport

    async def embed(self, texts):
        settings = self.settings
        ark = settings.embedding_provider == "volcengine"
        model = settings.ark_embedding_model if ark else settings.embedding_model
        key = settings.ark_api_key if ark else settings.embedding_api_key
        base = settings.ark_base_url if ark else settings.embedding_base_url
        texts = [redact(t) if settings.redact_pii else t for t in texts]
        headers = {"Authorization": "Bearer " + key.get_secret_value()}
        async with httpx.AsyncClient(timeout=45, transport=self.transport) as client:

            async def request(path, body, parse):
                for attempt in range(3):
                    tick, data, success = time.monotonic(), {}, False
                    try:
                        response = await client.post(base.rstrip("/") + path, headers=headers, json=body)
                        if response.status_code == 429 or response.status_code >= 500:
                            if attempt < 2:
                                await asyncio.sleep(2**attempt)
                                continue
                        if response.is_error:
                            raise HTTPException(502, f"Embedding接口返回HTTP {response.status_code}")
                        data = response.json()
                        result = parse(data)
                        success = True
                        return result
                    except (ValueError, KeyError, TypeError, AttributeError):
                        raise HTTPException(502, "Embedding返回向量或页序无效") from None
                    except httpx.HTTPError:
                        if attempt == 2:
                            raise HTTPException(502, "Embedding连接失败或超时") from None
                        await asyncio.sleep(2**attempt)
                    finally:
                        usage = data.get("usage", {}) if isinstance(data, dict) else {}
                        tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
                        tokens = tokens if type(tokens) is int and 0 <= tokens <= 2147483647 else None
                        cost = (
                            tokens * settings.embedding_price_per_million / 1_000_000
                            if tokens is not None and settings.embedding_price_per_million is not None
                            else None
                        )
                        await record_call(
                            stage="Embedding",
                            model=model,
                            duration_ms=(time.monotonic() - tick) * 1000,
                            input_tokens=tokens,
                            output_tokens=None,
                            cost=cost,
                            success=success,
                            usage_source="provider" if tokens is not None else "unavailable",
                            modality="embedding",
                            details={
                                "provider": settings.embedding_provider,
                                "currency": settings.cost_currency,
                            },
                        )

            if ark:
                semaphore = asyncio.Semaphore(settings.embedding_concurrency)

                async def one(text):
                    async with semaphore:
                        body = {
                            "model": model,
                            "input": [{"type": "text", "text": text}],
                            "encoding_format": "float",
                        }
                        if settings.ark_embedding_dimensions is not None:
                            body["dimensions"] = settings.ark_embedding_dimensions
                        # Multimodal inputs form one combined embedding, not an independent batch.
                        return await request(
                            "/embeddings/multimodal",
                            body,
                            lambda data: valid_vector(data["data"]["embedding"]),
                        )

                tasks = [asyncio.create_task(one(text)) for text in texts]
                try:
                    vectors = await asyncio.gather(*tasks)
                except BaseException:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    raise
            else:
                vectors = []
                for start in range(0, len(texts), settings.embedding_batch_size):
                    batch = texts[start : start + settings.embedding_batch_size]

                    def parse(data):
                        items = sorted(data["data"], key=lambda item: item["index"])
                        if [i["index"] for i in items] != list(range(len(batch))):
                            raise ValueError("invalid indices")
                        return [valid_vector(item["embedding"]) for item in items]

                    vectors.extend(await request("/embeddings", {"model": model, "input": batch}, parse))
        if vectors and any(len(v) != len(vectors[0]) for v in vectors):
            raise HTTPException(502, "Embedding向量维度不一致，请核对模型后重新索引")
        return vectors
