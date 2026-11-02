import re

import httpx

from app.core.telemetry import runtime


class SafetyRejection(RuntimeError):
    pass


async def check_content(settings, text, stage, client=None):
    """Local rules plus optional review gateway. Never claims local regex is full moderation.

    Gateway contract: POST {text, stage}, response {allowed: bool, categories: [str]}.
    Native cloud provider signatures belong in a separate gateway adapter.
    """
    verdict, reasons = "allowed_local", []
    if any(term.casefold() in text.casefold() for term in settings.content_blocked_terms if term):
        verdict, reasons = "blocked", ["configured_blocked_term"]
    if verdict != "blocked" and settings.content_review_url:
        own_client = client is None
        client = client or httpx.AsyncClient(timeout=20, follow_redirects=False)
        try:
            response = await client.post(
                settings.content_review_url,
                headers={"Authorization": "Bearer " + settings.content_review_key.get_secret_value()},
                json={"text": text, "stage": stage},
            )
            if response.is_error:
                raise SafetyRejection("内容审查服务暂时不可用，未继续生成")
            data = response.json()
            if type(data.get("allowed")) is not bool:
                raise SafetyRejection("内容审查服务返回无效结果，未继续生成")
            verdict = "allowed_remote" if data["allowed"] else "blocked"
            reasons = ["remote_policy"] if not data["allowed"] else []
        except (httpx.HTTPError, ValueError, TypeError):
            raise SafetyRejection("内容审查服务连接或响应异常，未继续生成") from None
        finally:
            if own_client:
                await client.aclose()
    context = runtime.get()
    if context and context.get("audit"):
        await context["audit"](
            {"stage": stage, "verdict": verdict, "detail": {"reasons": reasons, "characters": len(text)}}
        )
    if verdict == "blocked":
        raise SafetyRejection("内容未通过审查，请修改相关内容后重试")


def secrets_filter(text):
    # Also catches common assignment forms; no raw secret is stored in audit records.
    return re.sub(
        r'(?i)(api[_-]?key|authorization|access[_-]?token)(["\s:=]+)([A-Za-z0-9_./+-]{16,})',
        r"\1\2[密钥已隐藏]",
        text,
    )
