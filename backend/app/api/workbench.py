import asyncio
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.api.routes import idempotency_key, job_view, workflow
from app.core.security import auth
from app.models import CallMetric, Event, Job, Task
from app.schemas.content import CardPack, StrictModel, TopicReview
from app.services.workflow_service import dump_task, fingerprint

router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)])


class SessionPatch(StrictModel):
    title: str | None = Field(None, min_length=1, max_length=200)
    archived: bool | None = None


class Rerender(StrictModel):
    pack: CardPack | None = None
    theme: str | None = Field(None, pattern="^(clean|dark)$")
    page: int | None = Field(None, ge=1, le=5)


class Fork(StrictModel):
    checkpoint_id: str = Field(min_length=1, max_length=128)


@router.patch("/tasks/{task_id}", tags=["会话"])
async def patch_session(task_id: str, body: SessionPatch, request: Request):
    await workflow(request).get(task_id, write=True)
    async with request.app.state.db.sessions() as session, session.begin():
        row = await session.get(Task, task_id)
        if body.title is not None:
            row.title = body.title
        if body.archived is not None:
            row.archived = body.archived
        await session.flush()
        await session.refresh(row)
        return dump_task(row)


@router.post("/tasks/{task_id}/rerender", status_code=202, tags=["内容资产"])
async def rerender(task_id: str, body: Rerender, request: Request, key: str = Depends(idempotency_key)):
    return job_view(
        await workflow(request).enqueue(
            task_id, "rerender", body.model_dump(mode="json", exclude_none=True), key
        )
    )


@router.get("/tasks/{task_id}/metrics", tags=["指标"])
async def metrics(task_id: str, request: Request):
    await workflow(request).get(task_id)
    async with request.app.state.db.sessions() as session:
        calls = (
            await session.scalars(
                select(CallMetric).where(CallMetric.task_id == task_id).order_by(CallMetric.created_at)
            )
        ).all()
    items = [
        {
            "id": c.id,
            "stage": c.stage,
            "model": c.model,
            "duration_ms": c.duration_ms,
            "input_tokens": c.input_tokens,
            "output_tokens": c.output_tokens,
            "estimated_cost": c.cost,
            "success": c.success,
            "usage_source": c.usage_source,
            "modality": c.modality,
            "details": c.details,
            "created_at": c.created_at,
        }
        for c in calls
    ]
    text_calls = [c for c in calls if c.modality == "text" and c.usage_source != "cache"]
    return {
        "items": items,
        "summary": {
            "calls": len(items),
            "text_calls": sum(c.modality == "text" for c in calls),
            "image_calls": sum(c.modality == "image" for c in calls),
            "cache_hits": sum(c.usage_source == "cache" for c in calls),
            "input_tokens": sum(c.input_tokens or 0 for c in text_calls)
            if text_calls and all(c.input_tokens is not None for c in text_calls)
            else None,
            "output_tokens": sum(c.output_tokens or 0 for c in text_calls)
            if text_calls and all(c.output_tokens is not None for c in text_calls)
            else None,
            "estimated_cost": sum(c.cost or 0 for c in calls)
            if calls and all(c.cost is not None for c in calls)
            else None,
            "failures": sum(not c.success for c in calls),
        },
    }


@router.get("/tasks/{task_id}/checkpoints", tags=["会话"])
async def checkpoints(task_id: str, request: Request, limit: int = Query(50, ge=1, le=100)):
    task = await workflow(request).get(task_id)
    result = []
    async for snapshot in request.app.state.runner.graph.aget_state_history(
        {"configurable": {"thread_id": task.thread_id}}, limit=limit
    ):
        state = snapshot.values
        result.append(
            {
                "id": snapshot.config["configurable"]["checkpoint_id"],
                "created_at": snapshot.created_at,
                "next": list(snapshot.next),
                "article_version": state.get("article_version", 0),
                "title": state.get("selected_topic", {}).get("title"),
                "has_article": bool(state.get("article_draft")),
                "ready": state.get("ready", False),
            }
        )
    return result


@router.post("/tasks/{task_id}/fork", status_code=202, tags=["会话"])
async def fork(task_id: str, body: Fork, request: Request, key: str = Depends(idempotency_key)):
    source = await workflow(request).get(task_id, write=True)
    snapshot = await request.app.state.runner.graph.aget_state(
        {"configurable": {"thread_id": source.thread_id, "checkpoint_id": body.checkpoint_id}}
    )
    if not snapshot.values:
        raise HTTPException(404, "历史检查点不存在或尚无业务状态")
    scoped = fingerprint({"owner": request.state.owner_id, "key": key, "action": "fork"})
    digest = fingerprint({"task": task_id, "checkpoint": body.checkpoint_id})
    async with request.app.state.db.sessions() as session:
        existing = await session.scalar(select(Task).where(Task.request_key == scoped))
        if existing:
            if existing.request_hash != digest:
                raise HTTPException(409, "幂等键已用于其他历史版本")
            return dump_task(existing)
        row = Task(
            id=str(uuid4()),
            owner_id=source.owner_id,
            title=source.title[:190] + " · 副本",
            request_key=scoped,
            request_hash=digest,
            input=source.input,
        )
        session.add(row)
        try:
            await session.flush()
            session.add(
                Job(
                    task_id=row.id,
                    kind="fork",
                    request_key=scoped,
                    request_hash=digest,
                    payload={
                        "state": dict(snapshot.values),
                        "origin_task": task_id,
                        "origin_checkpoint": body.checkpoint_id,
                    },
                )
            )
            session.add(
                Event(
                    task_id=row.id,
                    kind="forked",
                    detail={"source_task": task_id, "checkpoint": body.checkpoint_id},
                )
            )
            await session.commit()
        except IntegrityError:
            raise HTTPException(409, "相同复制操作正在处理，请刷新任务列表") from None
        return dump_task(row)


@router.get("/tasks/{task_id}/stream", tags=["流式进度"])
async def stream(
    task_id: str,
    request: Request,
    after_id: int = Query(0, ge=0),
    last_event_id: str | None = Header(None, alias="Last-Event-ID"),
):
    await workflow(request).get(task_id)
    if last_event_id:
        try:
            after_id = max(after_id, int(last_event_id))
        except ValueError:
            raise HTTPException(422, "Last-Event-ID 必须是事件序号") from None

    async def events():
        cursor = after_id
        idle = 0
        while not await request.is_disconnected():
            async with request.app.state.db.sessions() as session:
                rows = (
                    await session.scalars(
                        select(Event)
                        .where(Event.task_id == task_id, Event.id > cursor)
                        .order_by(Event.id)
                        .limit(100)
                    )
                ).all()
                task = await session.get(Task, task_id)
                active = await session.scalar(
                    select(Job.id).where(Job.task_id == task_id, Job.status.in_(["queued", "running"]))
                )
            for row in rows:
                cursor = row.id
                payload = {"type": row.kind, "data": row.detail, "id": cursor}
                yield f"id: {cursor}\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"
            if not active and len(rows) < 100:
                yield (
                    "data: "
                    + json.dumps({"type": "done", "data": {"status": task.status}, "id": cursor})
                    + "\n\n"
                )
                return
            idle += 1
            if idle % 25 == 0:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.2)

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/dashboard", tags=["指标"])
async def dashboard(request: Request):
    async with request.app.state.db.sessions() as session:
        tasks = (
            await session.scalars(select(Task).where(Task.owner_id == request.state.owner_id))
        ).all()
        reviews = [r for t in tasks for r in t.snapshot.get("review_history", [])]
        rejected = sum(r["decision"] == "rejected" for r in reviews)
        selected = sum(bool(t.snapshot.get("selected_topic")) for t in tasks)
        article_count = sum(bool(t.snapshot.get("article_draft")) for t in tasks)
        revisions = sum(t.snapshot.get("revision_count", 0) for t in tasks)
        from app.models import GenerationRating

        ratings = (
            await session.scalars(
                select(GenerationRating).where(GenerationRating.owner_id == request.state.owner_id)
            )
        ).all()
        versions = {}
        for r in ratings:
            version = r.prompt_snapshot.get("Article", {}).get("digest", "legacy")
            versions.setdefault(version, []).append(r.score)
        return {
            "tasks": len(tasks),
            "ready": sum(t.status == "READY" for t in tasks),
            "waiting_review": sum(t.status == "WAITING_ARTICLE_REVIEW" for t in tasks),
            "failed": sum(t.status == "FAILED" for t in tasks),
            "revisions": revisions,
            "selection_rate": selected / len(tasks) if tasks else None,
            "review_rejection_rate": rejected / len(reviews) if reviews else None,
            "average_revisions": revisions / article_count if article_count else None,
            "average_rating": sum(r.score for r in ratings) / len(ratings) if ratings else None,
            "rating_count": len(ratings),
            "prompt_ratings": [
                {"digest": k, "count": len(v), "average": sum(v) / len(v)} for k, v in versions.items()
            ],
            "funnel": {
                "created": len(tasks),
                "topics": sum(bool(t.snapshot.get("candidate_topics")) for t in tasks),
                "selected": selected,
                "article": article_count,
                "approved": sum(bool(t.snapshot.get("approved_article")) for t in tasks),
                "ready": sum(t.status == "READY" for t in tasks),
            },
        }


@router.post("/tasks/{task_id}/regenerate-topics", status_code=202, tags=["工作流"])
async def regenerate_topics(task_id: str, request: Request, key: str = Depends(idempotency_key)):
    return job_view(await workflow(request).enqueue(task_id, "regenerate_topics", {}, key))


@router.post("/tasks/{task_id}/review-topic", status_code=202, tags=["审核协作"])
async def review_topic(
    task_id: str, body: TopicReview, request: Request, key: str = Depends(idempotency_key)
):
    from app.api.routes import identity_body

    return job_view(
        await workflow(request).enqueue(task_id, "topic_review", identity_body(request, body), key)
    )
