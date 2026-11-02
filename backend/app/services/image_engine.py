import asyncio
import base64
import binascii
import io
import json
import time
from urllib.parse import urlparse

import httpx
from PIL import Image

from app.agents.tools.image_renderer.html_playwright import CardRenderer
from app.core.content_safety import check_content, secrets_filter
from app.core.llm_factory import ModelOutputError
from app.core.telemetry import emit, record_call, redact


class APIImageRenderer:
    def __init__(self, settings, storage, transport=None):
        self.settings, self.storage, self.transport = settings, storage, transport

    async def render(self, task_id, version, pack, theme, mode, page_numbers=None):
        ark = self.settings.image_provider == "volcengine"
        key = (self.settings.ark_api_key if ark else self.settings.image_api_key).get_secret_value()
        model = self.settings.ark_image_model if ark else self.settings.image_model
        base_url = self.settings.ark_base_url if ark else self.settings.image_base_url
        if not key or not model:
            raise ModelOutputError("请在本地配置图片服务的 IMAGE_API_KEY 和 IMAGE_MODEL")
        semaphore = asyncio.Semaphore(self.settings.image_concurrency)
        async with httpx.AsyncClient(
            timeout=self.settings.image_timeout_seconds, transport=self.transport
        ) as client:

            async def one(index, slide):
                async with semaphore:
                    prompt = (
                        f"生成同一系列第{index + 1}/{len(pack['slides'])}张3:4竖版中文技术知识卡片。"
                        "只生成一张图片，保持系列字体、配色和版式统一。"
                        "逐字准确呈现标题、要点与代码，保留代码缩进，严格忠于以下审核内容，不增加事实。"
                        + ("视觉使用深色技术风格。\n" if theme == "dark" else "视觉使用清爽浅色风格。\n")
                        + json.dumps(slide, ensure_ascii=False)
                    )
                    if self.settings.redact_pii:
                        prompt = secrets_filter(redact(prompt))
                    await check_content(self.settings, prompt, "image:input")
                    headers = (
                        {"x-goog-api-key": key}
                        if not ark and self.settings.image_auth == "x-goog-api-key"
                        else {"Authorization": "Bearer " + key}
                    )
                    if self.settings.image_provider == "gemini":
                        url = base_url.rstrip("/") + f"/v1beta/models/{model}:generateContent"
                        body = {
                            "contents": [{"parts": [{"text": prompt}]}],
                            "generationConfig": {
                                "responseModalities": ["IMAGE", "TEXT"],
                                "imageConfig": {"aspectRatio": "3:4"},
                            },
                        }
                    elif ark:
                        url = base_url.rstrip("/") + "/images/generations"
                        # Ark does not use OpenAI's n parameter. One request per reviewed slide;
                        # omit sequential_image_generation for models that do not accept it.
                        body = {
                            "model": model,
                            "prompt": prompt,
                            "size": self.settings.ark_image_size,
                            "response_format": self.settings.ark_image_response_format,
                            "stream": False,
                            "watermark": self.settings.ark_image_watermark,
                        }
                    else:
                        url = base_url.rstrip("/") + "/images/generations"
                        body = {
                            "model": model,
                            "prompt": prompt,
                            "size": self.settings.image_size,
                            "n": 1,
                        }
                    for attempt in range(3):
                        tick, success = time.monotonic(), False
                        try:
                            response = await client.post(url, headers=headers, json=body)
                            if response.status_code == 429 or response.status_code >= 500:
                                if attempt < 2:
                                    await asyncio.sleep(2**attempt)
                                    continue
                            if response.is_error:
                                raise ModelOutputError(
                                    f"图片服务返回 HTTP {response.status_code}；请检查配置或修改被拒绝的内容"
                                )
                            data = response.json()
                            if not isinstance(data, dict) or data.get("error"):
                                raise ModelOutputError("图片服务未生成图片，请检查模型权限或内容后重试")
                            if self.settings.image_provider == "gemini":
                                parts = (
                                    data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                                )
                                encoded = next(
                                    (
                                        p.get("inlineData", p.get("inline_data", {})).get("data")
                                        for p in parts
                                        if "inlineData" in p or "inline_data" in p
                                    ),
                                    None,
                                )
                                if not encoded:
                                    raise ModelOutputError(
                                        "图片服务没有返回图片，可能被内容策略拒绝；请修改后再试"
                                    )
                                binary = base64.b64decode(encoded, validate=True)
                            else:
                                items = data.get("data")
                                if not isinstance(items, list) or len(items) != 1:
                                    raise ModelOutputError(
                                        "图片服务未按单页请求返回一张图片，请检查模型配置"
                                    )
                                item = items[0]
                                if not isinstance(item, dict) or item.get("error"):
                                    raise ModelOutputError(
                                        f"第 {index + 1} 页图片生成失败或被拒绝，请修改后重试"
                                    )
                                if item.get("b64_json"):
                                    binary = base64.b64decode(item["b64_json"], validate=True)
                                elif item.get("url"):
                                    binary = await self._download(client, item["url"])
                                else:
                                    raise ModelOutputError("图片服务没有返回可读取的图片数据")
                            if len(binary) > 20_000_000:
                                raise ModelOutputError("图片文件超过20MB")
                            image = Image.open(io.BytesIO(binary))
                            if image.width * 4 != image.height * 3:
                                raise ModelOutputError(
                                    "图片服务未返回3:4比例，请调整供应商参数；未对原图进行静默裁切"
                                )
                            output = io.BytesIO()
                            image.convert("RGB").save(output, format="PNG")
                            await emit(
                                "image_completed",
                                {
                                    "page": index + 1,
                                    "total": len(pack["slides"]),
                                    "provider": self.settings.image_provider,
                                    "width": image.width,
                                    "height": image.height,
                                },
                            )
                            success = True
                            return self.storage.write(
                                f"{task_id}/v{version}/{theme}/card-{index + 1:02}.png",
                                output.getvalue(),
                                "xhs_image",
                                "image/png",
                            )
                        except httpx.TransportError:
                            if attempt == 2:
                                raise ModelOutputError("图片服务连接失败或超时") from None
                            await asyncio.sleep(2**attempt)
                        except (ValueError, KeyError, IndexError, TypeError, OSError, binascii.Error):
                            raise ModelOutputError(
                                "图片服务响应格式或图片文件无效，请检查模型与接口配置"
                            ) from None
                        finally:
                            await record_call(
                                stage=f"Image:{index + 1}",
                                model=model,
                                duration_ms=(time.monotonic() - tick) * 1000,
                                input_tokens=None,
                                output_tokens=None,
                                cost=self.settings.image_price_per_image if success else None,
                                success=success,
                                usage_source="configured_image_price"
                                if success and self.settings.image_price_per_image is not None
                                else "unavailable",
                                modality="image",
                                details={
                                    "page": index + 1,
                                    "provider": self.settings.image_provider,
                                    "currency": self.settings.cost_currency,
                                    "attempt": attempt + 1,
                                },
                            )

            # gather returns the original page order, even when requests finish out of order.
            tasks = [
                asyncio.create_task(one(i, slide))
                for i, slide in enumerate(pack["slides"])
                if page_numbers is None or i + 1 in page_numbers
            ]
            try:
                return await asyncio.gather(*tasks)
            except BaseException:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise

    @staticmethod
    async def _download(client, url):
        import ipaddress
        import socket

        target = urlparse(url)
        if target.scheme != "https" or not target.hostname or target.username:
            raise ModelOutputError("图片服务返回了无效下载地址")
        addresses = await asyncio.to_thread(socket.getaddrinfo, target.hostname, 443)
        if any(not ipaddress.ip_address(address[4][0]).is_global for address in addresses):
            raise ModelOutputError("图片下载地址不能指向本地或私有网络")
        async with client.stream("GET", url, follow_redirects=False) as response:
            if response.status_code != 200:
                raise ModelOutputError("图片下载失败")
            chunks, length = [], 0
            async for chunk in response.aiter_bytes():
                length += len(chunk)
                if length > 20_000_000:
                    raise ModelOutputError("图片文件超过20MB")
                chunks.append(chunk)
            return b"".join(chunks)


def image_engine(settings, storage):
    return (
        CardRenderer(storage)
        if settings.image_provider == "template"
        else APIImageRenderer(settings, storage)
    )
