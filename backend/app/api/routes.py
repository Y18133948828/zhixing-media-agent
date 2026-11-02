import io
import json
import zipfile
from typing import Annotated
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import desc, func, or_, select
from sqlalchemy.dialects.postgresql import insert

from app.core.security import auth
from app.models import Asset, Event, Feedback, Job, Knowledge, Revision, Task
from app.schemas.content import FeedbackCreate, KnowledgeCreate, Review, SelectTopic, TaskCreate
from app.services.workflow_service import WorkflowService, dump_task


def workflow(request):
    return WorkflowService(request.app.state.db, request.state.owner_id)


def identity_body(request, body):
    value = body.model_dump(mode="json")
    if request.state.owner_id:
        value["operator"] = request.state.username
    return value


router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])


def idempotency_key(
    value: Annotated[str | None, Header(alias="Idempotency-Key", min_length=1, max_length=128)] = None,
):
    return value or str(uuid4())


def job_view(job):
    return {
        "id": job.id,
        "task_id": job.task_id,
        "kind": job.kind,
        "status": job.status,
        "attempts": job.attempts,
        "error": job.error,
        "created_at": job.created_at,
        "updated_at": job.updated_at,
    }


@router.get("/capabilities", tags=["配置"])
async def capabilities(request: Request):
    settings = request.app.state.settings
    return {
        "llm_provider": settings.llm_provider,
        "demo_mode": settings.llm_provider == "mock",
        "langgraph_version": "1.0.10",
        "checkpointer_backend": settings.checkpointer_backend,
        "image_size": settings.image_dimensions,
        "image_aspect_ratio": "3:4",
        "publishing": "export_only",
        "live_trends": False,
        "max_revisions": settings.max_revisions,
        "image_provider": settings.image_provider,
        "streaming": settings.llm_streaming and settings.llm_provider != "anthropic",
        "knowledge_retrieval": "vector_hybrid"
        if settings.embedding_provider != "disabled"
        else "lexical",
        "workflow_templates": ["xhs_wechat", "xhs_only", "wechat_only"],
        "multi_review": True,
        "tracing_enabled": settings.tracing_enabled,
    }


@router.post("/tasks", status_code=202, tags=["工作流"])
async def create_task(body: TaskCreate, request: Request, key: str = Depends(idempotency_key)):
    return dump_task(await workflow(request).create(body, key))


@router.get("/tasks", tags=["工作流"])
async def list_tasks(
    request: Request,
    status: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    archived: bool = False,
):
    async with request.app.state.db.sessions() as session:
        access = Task.owner_id == request.state.owner_id
        if request.state.owner_id:
            access = or_(access, Task.input["reviewer_ids"].contains([request.state.owner_id]))
        query = select(Task).where(access, Task.archived == archived).order_by(desc(Task.created_at))
        count = select(func.count()).select_from(Task).where(access, Task.archived == archived)
        if status:
            query, count = query.where(Task.status == status), count.where(Task.status == status)
        rows = (await session.scalars(query.offset(offset).limit(limit))).all()
        return {
            "total": await session.scalar(count),
            "items": [
                {
                    "id": t.id,
                    "owner_id": t.owner_id,
                    "status": t.status,
                    "session_title": t.title,
                    "thread_id": t.thread_id,
                    "course_category": t.input["course_category"],
                    "title": t.snapshot.get("selected_topic", {}).get("title"),
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                }
                for t in rows
            ],
        }


@router.get("/tasks/{task_id}", tags=["工作流"])
async def get_task(task_id: str, request: Request):
    return dump_task(await workflow(request).get(task_id))


@router.post("/tasks/{task_id}/select-topic", status_code=202, tags=["工作流"])
async def select_topic(
    task_id: str, body: SelectTopic, request: Request, key: str = Depends(idempotency_key)
):
    return job_view(
        await workflow(request).enqueue(task_id, "select", identity_body(request, body), key)
    )


@router.post("/tasks/{task_id}/review", status_code=202, tags=["工作流"])
async def review(task_id: str, body: Review, request: Request, key: str = Depends(idempotency_key)):
    return job_view(
        await workflow(request).enqueue(task_id, "review", identity_body(request, body), key)
    )


@router.post("/tasks/{task_id}/retry", status_code=202, tags=["工作流"])
async def retry(task_id: str, request: Request, key: str = Depends(idempotency_key)):
    return job_view(await workflow(request).enqueue(task_id, "retry", {}, key))


@router.get("/tasks/{task_id}/events", tags=["工作流"])
async def events(
    task_id: str, request: Request, after_id: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=500)
):
    await workflow(request).get(task_id)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(
                select(Event)
                .where(Event.task_id == task_id, Event.id > after_id)
                .order_by(Event.id)
                .limit(limit)
            )
        ).all()
        return [
            {"id": r.id, "kind": r.kind, "detail": r.detail, "created_at": r.created_at} for r in rows
        ]


@router.get("/tasks/{task_id}/revisions", tags=["内容资产"])
async def revisions(task_id: str, request: Request):
    await workflow(request).get(task_id)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(
                select(Revision).where(Revision.task_id == task_id).order_by(Revision.version)
            )
        ).all()
        return [{"version": r.version, "article": r.article, "decision": r.decision} for r in rows]


@router.get("/jobs/{job_id}", tags=["工作流"])
async def job(job_id: str, request: Request):
    async with request.app.state.db.sessions() as session:
        row = await session.get(Job, job_id)
        if row is None:
            raise HTTPException(404, "操作不存在")
        await workflow(request).get(row.task_id)
        return job_view(row)


@router.get("/tasks/{task_id}/assets", tags=["内容资产"])
async def assets(task_id: str, request: Request):
    await workflow(request).get(task_id)
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(select(Asset).where(Asset.task_id == task_id).order_by(Asset.path))
        ).all()
        return [
            {
                "id": a.id,
                "kind": a.kind,
                "filename": a.path.rsplit("/", 1)[-1],
                "sha256": a.sha256,
                "mime_type": a.mime_type,
                "download_url": f"/api/v1/assets/{a.id}/download",
            }
            for a in rows
        ]


@router.get("/assets/{asset_id}/download", tags=["内容资产"])
async def download(asset_id: str, request: Request):
    async with request.app.state.db.sessions() as session:
        asset = await session.get(Asset, asset_id)
        if asset is None:
            raise HTTPException(404, "资产不存在")
        await workflow(request).get(asset.task_id)
        path = request.app.state.storage.resolve(asset.path)
        if not path.is_file():
            raise HTTPException(404, "资产文件缺失，请重新运行生成任务")
        return FileResponse(
            path,
            media_type=asset.mime_type,
            filename=path.name,
            headers={"X-Content-Type-Options": "nosniff"},
        )


@router.get("/tasks/{task_id}/export", tags=["内容资产"])
async def export(task_id: str, request: Request):
    task = await workflow(request).get(task_id)
    if task.status != "READY":
        raise HTTPException(409, "内容尚未就绪")
    async with request.app.state.db.sessions() as session:
        rows = (
            await session.scalars(select(Asset).where(Asset.task_id == task_id).order_by(Asset.path))
        ).all()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for asset in rows:
            path = request.app.state.storage.resolve(asset.path)
            if not path.is_file():
                raise HTTPException(409, "资产文件缺失，无法导出")
            archive.write(path, path.name)
        archive.writestr(
            "manifest.json",
            json.dumps(
                {
                    "task_id": task_id,
                    "mode": task.snapshot.get("mode"),
                    "article_version": task.snapshot.get("article_version"),
                    "fact_checks": task.snapshot.get("approved_article", {}).get("fact_checks", []),
                    "files": [{"filename": a.path.rsplit("/", 1)[-1], "sha256": a.sha256} for a in rows],
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    return StreamingResponse(
        iter([buffer.getvalue()]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="media-{task_id}.zip"'},
    )


@router.post("/knowledge", status_code=201, tags=["选题素材"])
async def create_knowledge(body: KnowledgeCreate, request: Request):
    async with request.app.state.db.sessions() as session, session.begin():
        row = Knowledge(owner_id=request.state.owner_id, **body.model_dump(mode="json"))
        session.add(row)
        await session.flush()
        return {"id": row.id, **body.model_dump(mode="json")}


@router.get("/knowledge", tags=["选题素材"])
async def list_knowledge(
    request: Request, category: str | None = None, limit: int = Query(30, ge=1, le=100)
):
    async with request.app.state.db.sessions() as session:
        query = (
            select(Knowledge)
            .where(Knowledge.owner_id == request.state.owner_id)
            .order_by(desc(Knowledge.created_at))
            .limit(limit)
        )
        if category:
            query = query.where(Knowledge.category == category)
        rows = (await session.scalars(query)).all()
        return [
            {
                "id": r.id,
                "category": r.category,
                "title": r.title,
                "body": r.body,
                "source_url": r.source_url,
            }
            for r in rows
        ]


@router.put("/tasks/{task_id}/feedback", tags=["数据反馈"])
async def feedback(task_id: str, body: FeedbackCreate, request: Request):
    task = await workflow(request).get(task_id, write=True)
    if task.status != "READY":
        raise HTTPException(409, "请为已完成的内容录入平台数据")
    data = body.model_dump()
    async with request.app.state.db.sessions() as session, session.begin():
        await session.execute(
            insert(Feedback)
            .values(task_id=task_id, **data)
            .on_conflict_do_update(index_elements=[Feedback.task_id, Feedback.platform], set_=data)
        )
    return {"task_id": task_id, **data}


@router.get("/tasks/{task_id}/feedback", tags=["数据反馈"])
async def get_feedback(task_id: str, request: Request):
    await workflow(request).get(task_id)
    async with request.app.state.db.sessions() as session:
        rows = (await session.scalars(select(Feedback).where(Feedback.task_id == task_id))).all()
        return [
            {
                "platform": r.platform,
                "views": r.views,
                "likes": r.likes,
                "saves": r.saves,
                "comments": r.comments,
                "observed_at": r.observed_at,
                "note": r.note,
            }
            for r in rows
        ]
