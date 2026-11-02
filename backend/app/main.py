import asyncio
import logging
import sys
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text

from app.api.auth import router as auth_router
from app.api.optimization import router as optimization_router
from app.api.routes import router
from app.api.sso import router as sso_router
from app.api.workbench import router as workbench_router
from app.core.config import BACKEND_ROOT, Settings
from app.core.database import Database
from app.core.llm_factory import LLM
from app.core.observability import configure_logging, request_middleware
from app.services.runner import Runner
from app.services.storage import Storage
from app.services.workflow_service import WorkflowService

# psycopg asynchronous connections require SelectorEventLoop on Windows.
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


def create_app(settings: Settings | None = None):
    settings = settings or Settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app):
        db = Database(settings)
        llm = LLM(settings)
        try:
            async with db.engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM media_tasks LIMIT 0"))
                if settings.checkpointer_backend == "postgres":
                    await connection.execute(text("SELECT 1 FROM langgraph.checkpoints LIMIT 0"))
            async with db.checkpointer() as checkpointer:
                app.state.db, app.state.settings = db, settings
                app.state.storage = Storage(settings.storage_dir)
                app.state.workflow = WorkflowService(db)
                app.state.runner = Runner(db, llm, app.state.storage, checkpointer)
                consumer = asyncio.create_task(app.state.runner.loop())
                app.state.runner_task = consumer
                logging.getLogger(__name__).warning(
                    "本地 Demo 已启动，模型模式：%s", settings.llm_provider
                )
                try:
                    yield
                finally:
                    consumer.cancel()
                    with suppress(asyncio.CancelledError):
                        await consumer
        finally:
            await llm.close()
            await db.close()

    app = FastAPI(
        title="AI教育自媒体运营 Demo",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
        description="选题 → 人工选择 → 长文 → 人工审核/重写 → 小红书卡片 + 公众号导出。所有写操作仅影响本地 Demo。",
    )
    app.state.settings = settings
    app.middleware("http")(request_middleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE"],
        allow_headers=["Content-Type", "X-API-Key", "Idempotency-Key", "Authorization", "Last-Event-ID"],
    )
    app.include_router(router)
    app.include_router(auth_router)
    app.include_router(workbench_router)
    app.include_router(optimization_router)
    app.include_router(sso_router)

    from fastapi.exceptions import RequestValidationError

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": list(e["loc"]), "msg": e["msg"], "type": e["type"]} for e in exc.errors()
                ],
                "request_id": getattr(request.state, "request_id", None),
            },
        )

    frontend_dist = BACKEND_ROOT.parent / "frontend/dist"
    if frontend_dist.is_dir():
        app.mount("/app", StaticFiles(directory=frontend_dist, html=True), name="workbench")

    @app.get("/", include_in_schema=False)
    async def home():
        from fastapi.responses import RedirectResponse

        return RedirectResponse("/app/" if frontend_dist.is_dir() else "/docs")

    app.mount(
        "/docs-assets", StaticFiles(directory=BACKEND_ROOT / "app/api/static/docs"), name="docs-assets"
    )

    @app.get("/docs", include_in_schema=False)
    async def docs():
        return get_swagger_ui_html(
            openapi_url="/openapi.json",
            title="AI教育自媒体运营 · API调试",
            swagger_js_url="/docs-assets/swagger-ui-bundle.js",
            swagger_css_url="/docs-assets/swagger-ui.css",
            swagger_favicon_url="/docs-assets/favicon-32x32.png",
            swagger_ui_parameters={"validatorUrl": None, "displayRequestDuration": True},
        )

    @app.get("/health/live", tags=["健康检查"])
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", tags=["健康检查"])
    async def ready():
        try:
            async with app.state.db.engine.connect() as connection:
                await connection.execute(text("SELECT 1 FROM media_tasks LIMIT 0"))
            if app.state.runner_task.done():
                return JSONResponse(status_code=503, content={"status": "worker_unavailable"})
            return {"status": "ok", "llm_provider": settings.llm_provider}
        except Exception:
            return JSONResponse(status_code=503, content={"status": "database_unavailable"})

    return app


app = create_app()
