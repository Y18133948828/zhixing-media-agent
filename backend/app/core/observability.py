import asyncio
import json
import logging
import re
import time
from contextlib import asynccontextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from uuid import uuid4

from fastapi.responses import JSONResponse
from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert

from app.models import RateBucket

correlation = ContextVar("media_correlation", default={})


class JSONLog(logging.Formatter):
    def format(self, record):
        return json.dumps(
            {
                "time": datetime.now(timezone.utc).isoformat(),
                "level": record.levelname,
                "logger": record.name,
                "message": record.getMessage(),
                **correlation.get(),
            },
            ensure_ascii=False,
        )


def configure_logging(settings):
    logger = logging.getLogger("app")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    target = str(settings.log_dir.resolve() / "media.jsonl")
    if any(getattr(h, "baseFilename", None) == target for h in logger.handlers):
        return
    settings.log_dir.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(target, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(JSONLog())
    logger.addHandler(handler)


async def request_middleware(request, call_next):
    settings = request.app.state.settings
    supplied = request.headers.get("X-Request-ID", "")
    request_id = supplied if re.fullmatch(r"[a-zA-Z0-9_-]{8,64}", supplied) else uuid4().hex
    request.state.request_id = request_id
    token = correlation.set({"request_id": request_id})
    tick, status = time.monotonic(), 500
    try:
        if request.url.path.startswith("/api/"):
            import hashlib

            auth_path = (
                request.url.path.startswith("/api/v1/auth/") and request.url.path != "/api/v1/auth/me"
            )
            limit = settings.login_limit_per_minute if auth_path else settings.rate_limit_per_minute
            key = hashlib.sha256(
                f"{request.client.host if request.client else 'local'}:{'auth' if auth_path else 'api'}".encode()
            ).hexdigest()
            window = int(time.time()) // 60
            async with request.app.state.db.sessions() as session, session.begin():
                statement = insert(RateBucket).values(key=key, window=window, count=1)
                count = await session.scalar(
                    statement.on_conflict_do_update(
                        index_elements=[RateBucket.key, RateBucket.window],
                        set_={"count": RateBucket.count + 1},
                    ).returning(RateBucket.count)
                )
                if count == 1:
                    await session.execute(delete(RateBucket).where(RateBucket.window < window - 1440))
            if count > limit:
                status = 429
                return JSONResponse(
                    status_code=429,
                    content={"detail": "请求过于频繁，请稍后再试", "request_id": request_id},
                    headers={"Retry-After": str(60 - int(time.time()) % 60), "X-Request-ID": request_id},
                )
        response = await call_next(request)
        status = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        logging.getLogger(__name__).info(
            json.dumps(
                {
                    "event": "http_request",
                    "method": request.method,
                    "path": request.url.path,
                    "status": status,
                    "duration_ms": round((time.monotonic() - tick) * 1000, 2),
                },
                ensure_ascii=False,
            )
        )
        correlation.reset(token)


@asynccontextmanager
async def trace_span(name, settings, inputs=None):
    """External tracing is opt-in. Failures never discard business output."""
    logger = logging.getLogger(__name__)
    parent = correlation.get()
    run_id = str(uuid4())
    token = correlation.set({**parent, "trace_id": run_id})
    client = None
    try:
        if settings.tracing_enabled:
            from langsmith import Client

            client = Client(
                api_key=settings.tracing_api_key.get_secret_value(),
                api_url=settings.tracing_endpoint,
                timeout_ms=3000,
                auto_batch_tracing=False,
            )
            try:
                await asyncio.to_thread(
                    client.create_run,
                    name=name,
                    inputs=inputs or {},
                    run_type="chain",
                    id=run_id,
                    parent_run_id=parent.get("trace_id"),
                    project_name=settings.tracing_project,
                    extra={"metadata": parent},
                )
            except Exception:
                logger.warning("外部追踪写入失败")
        try:
            yield run_id
        except BaseException as exc:
            if client:
                try:
                    await asyncio.to_thread(
                        client.update_run,
                        run_id,
                        end_time=datetime.now(timezone.utc),
                        error=type(exc).__name__,
                    )
                except Exception:
                    logger.warning("外部追踪结束记录失败")
            raise
        else:
            if client:
                try:
                    await asyncio.to_thread(
                        client.update_run,
                        run_id,
                        end_time=datetime.now(timezone.utc),
                        outputs={"completed": True},
                    )
                except Exception:
                    logger.warning("外部追踪结束记录失败")
    finally:
        if client:
            try:
                await asyncio.to_thread(client.close)
            except Exception:
                logger.warning("外部追踪客户端关闭失败")
        correlation.reset(token)
