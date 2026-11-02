import hashlib
import json
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import desc, select
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.models import Event, Feedback, GenerationRating, Job, Knowledge, Task, User
from app.schemas.content import Review, SelectTopic, TaskCreate, TopicReview


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def dump_task(task: Task):
    return {
        "id": task.id,
        "owner_id": task.owner_id,
        "thread_id": task.thread_id,
        "status": task.status,
        "title": task.title,
        "archived": task.archived,
        "input": task.input,
        "state": task.snapshot,
        "interrupt_id": task.interrupt_id,
        "checkpoint_id": task.checkpoint_id,
        "last_error": task.last_error,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


class WorkflowService:
    def __init__(self, db: Database, owner_id: str | None = None):
        self.db = db
        self.owner_id = owner_id

    async def get(self, task_id: str, *, write=False):
        async with self.db.sessions() as session:
            task = await session.get(Task, task_id)
            if task is None or (
                task.owner_id != self.owner_id
                and (write or self.owner_id not in task.input.get("reviewer_ids", []))
            ):
                raise HTTPException(404, "任务不存在")
            return task

    async def create(self, request: TaskCreate, request_key: str):
        payload = request.model_dump(mode="json")
        if self.owner_id:
            request_key = fingerprint({"owner": self.owner_id, "key": request_key})
        digest = fingerprint(payload)
        async with self.db.sessions() as session:
            existing = await session.scalar(
                select(Task).where(Task.request_key == request_key, Task.owner_id == self.owner_id)
            )
            if existing:
                if existing.request_hash != digest:
                    raise HTTPException(409, "同一 Idempotency-Key 不能用于不同内容")
                return existing
            # A bounded snapshot of inputs: current jobs do not change when knowledge is edited later.
            knowledge = (
                await session.scalars(
                    select(Knowledge)
                    .where(
                        Knowledge.category == request.course_category,
                        Knowledge.owner_id == self.owner_id,
                    )
                    .order_by(desc(Knowledge.created_at))
                    .limit(200)
                )
            ).all()
            rows = (
                await session.execute(
                    select(Feedback, Task)
                    .join(Task, Feedback.task_id == Task.id)
                    .where(
                        Task.input["course_category"].astext == request.course_category,
                        Task.owner_id == self.owner_id,
                    )
                    .order_by(desc(Feedback.observed_at))
                    .limit(30)
                )
            ).all()
            from app.services.retrieval import retrieve

            knowledge = retrieve(knowledge, request.brief + " " + request.target_audience)
            from app.services.prompt_store import BUILTINS, snapshots

            if set(request.prompt_ids) - set(BUILTINS):
                raise HTTPException(422, "提示词阶段只能为TopicBatch、Article、CardPack")
            payload["prompt_snapshots"] = await snapshots(
                session, self.owner_id, request_key, request.prompt_ids, self.db.settings
            )
            payload["prompt_version"] = fingerprint(payload["prompt_snapshots"])
            payload["approval_steps"] = []
            if request.reviewer_ids:
                if (
                    not self.owner_id
                    or len(set(request.reviewer_ids)) != len(request.reviewer_ids)
                    or self.owner_id in request.reviewer_ids
                ):
                    raise HTTPException(422, "多级审核需要登录，审核人不能重复或与创建者相同")
                owner = await session.get(User, self.owner_id)
                payload["approval_steps"].append(
                    {"reviewer_id": owner.id, "username": owner.username, "role": owner.role}
                )
                for i, reviewer_id in enumerate(request.reviewer_ids):
                    user = await session.get(User, reviewer_id)
                    allowed = {"manager", "director", "admin"} if i == 0 else {"director", "admin"}
                    if not user or user.disabled or user.role not in allowed:
                        raise HTTPException(422, "审核人角色不符合该级审核要求")
                    payload["approval_steps"].append(
                        {"reviewer_id": user.id, "username": user.username, "role": user.role}
                    )
            recent = (
                await session.scalars(
                    select(Task)
                    .where(
                        Task.owner_id == self.owner_id,
                        Task.input["course_category"].astext == request.course_category,
                    )
                    .order_by(Task.created_at.desc())
                    .limit(30)
                )
            ).all()
            ratings = (
                await session.execute(
                    select(GenerationRating, Task)
                    .join(Task, GenerationRating.task_id == Task.id)
                    .where(
                        GenerationRating.owner_id == self.owner_id,
                        Task.input["course_category"].astext == request.course_category,
                    )
                    .order_by(GenerationRating.created_at.desc())
                    .limit(20)
                )
            ).all()
            context = {
                "knowledge": [
                    {"id": k.id, "title": k.title, "body": k.body[:4000], "source_url": k.source_url}
                    for k in knowledge
                ],
                "performance_samples": [
                    {
                        "topic": t.snapshot.get("selected_topic", {}).get("title"),
                        "platform": f.platform,
                        "views": f.views,
                        "likes": f.likes,
                        "saves": f.saves,
                        "comments": f.comments,
                        "save_rate": f.saves / f.views if f.views else None,
                        "like_rate": f.likes / f.views if f.views else None,
                        "observed_at": f.observed_at.isoformat(),
                        "note": f.note,
                    }
                    for f, t in rows
                ],
                "has_live_trend_data": False,
                "retrieval": {"mode": "lexical"},
                "recent_topics": [
                    t.snapshot["selected_topic"]["title"]
                    for t in recent
                    if t.snapshot.get("selected_topic")
                ],
                "quality_feedback": [
                    {
                        "topic": t.snapshot.get("selected_topic", {}).get("title"),
                        "score": r.score,
                        "dimensions": r.dimensions,
                        "comment": r.comment,
                    }
                    for r, t in ratings
                ],
            }
            task = Task(
                id=str(uuid4()),
                owner_id=self.owner_id,
                title=request.brief[:80],
                request_key=request_key,
                request_hash=digest,
                input=payload,
            )
            session.add(task)
            try:
                await session.flush()
                session.add(
                    Job(
                        task_id=task.id,
                        kind="start",
                        request_key=request_key,
                        request_hash=digest,
                        payload={"context": context},
                    )
                )
                session.add(
                    Event(
                        task_id=task.id, kind="created", detail={"mode": self.db.settings.llm_provider}
                    )
                )
                await session.commit()
                return task
            except IntegrityError:
                await session.rollback()
                existing = await session.scalar(
                    select(Task).where(Task.request_key == request_key, Task.owner_id == self.owner_id)
                )
                if existing and existing.request_hash == digest:
                    return existing
                raise HTTPException(409, "创建请求冲突，请更换 Idempotency-Key") from None

    async def enqueue(self, task_id: str, kind: str, payload: dict, key: str):
        digest = fingerprint({"kind": kind, "payload": payload})
        async with self.db.sessions() as session, session.begin():
            task = await session.scalar(select(Task).where(Task.id == task_id).with_for_update())
            if task is None or (
                task.owner_id != self.owner_id
                and (
                    kind not in {"review", "topic_review"}
                    or self.owner_id not in task.input.get("reviewer_ids", [])
                )
            ):
                raise HTTPException(404, "任务不存在")
            old = await session.scalar(select(Job).where(Job.task_id == task_id, Job.request_key == key))
            if old:
                if old.request_hash != digest:
                    raise HTTPException(409, "同一 Idempotency-Key 不能用于不同操作")
                return old
            active = await session.scalar(
                select(Job.id).where(Job.task_id == task_id, Job.status.in_(["queued", "running"]))
            )
            if active:
                raise HTTPException(409, "任务正在执行，请等待当前操作完成")
            if kind == "select":
                request = SelectTopic.model_validate(payload)
                self.validate_interrupt(task, request.interrupt_id, "WAITING_TOPIC_SELECTION")
                if request.topic_id not in {t["id"] for t in task.snapshot.get("candidate_topics", [])}:
                    raise HTTPException(422, "选题不在候选列表内")
            elif kind in {"review", "topic_review"}:
                request = (
                    Review.model_validate(payload)
                    if kind == "review"
                    else TopicReview.model_validate(payload)
                )
                self.validate_interrupt(
                    task,
                    request.interrupt_id,
                    "WAITING_ARTICLE_REVIEW" if kind == "review" else "WAITING_TOPIC_REVIEW",
                )
                reviewer = (
                    task.snapshot.get("pending_review", {})
                    .get("reviewer", {})
                    .get("reviewer_id", task.owner_id)
                )
                if reviewer != self.owner_id:
                    raise HTTPException(403, "请等待当前指定的审核人完成审核")
                if self.owner_id != task.owner_id:
                    user = await session.get(User, self.owner_id)
                    index = task.snapshot.get("pending_review", {}).get("review_index", 1)
                    roles = {"director", "admin"} if index >= 2 else {"manager", "director", "admin"}
                    if not user or user.disabled or user.role not in roles:
                        raise HTTPException(403, "指定审核人当前角色不能执行该级审核")
                if kind == "topic_review" and request.topic_round != task.snapshot.get("topic_round", 0):
                    raise HTTPException(409, "选题轮次已变化，请刷新")
                if kind == "review" and request.article_version != task.snapshot.get("article_version"):
                    raise HTTPException(409, "文案版本已更新，请刷新后审核")
                if (
                    kind == "review"
                    and request.decision == "rejected"
                    and task.snapshot.get("revision_count", 0) >= self.db.settings.max_revisions
                ):
                    raise HTTPException(409, "已达到重写次数上限；可提交手工定稿或创建新任务")
            elif kind == "retry":
                if task.status != "FAILED":
                    raise HTTPException(409, "只有失败任务可以重试")
                failed = await session.scalar(
                    select(Job)
                    .where(Job.task_id == task_id, Job.status == "failed")
                    .order_by(desc(Job.created_at))
                )
                if failed is None:
                    raise HTTPException(409, "没有可重试的操作")
                # Flatten retry chains; reuse the same checkpoint precondition.
                payload = {
                    "original_kind": failed.payload.get("original_kind", failed.kind),
                    "original_payload": failed.payload.get("original_payload", failed.payload),
                }
            elif kind == "rerender":
                if (
                    task.status not in {"READY", "FAILED"}
                    or not task.snapshot.get("approved_article")
                    or not task.snapshot.get("xhs_pack")
                ):
                    raise HTTPException(409, "请先完成文案审核和卡片提炼")
                page = payload.get("page")
                if page:
                    pack = task.snapshot["xhs_pack"]
                    images = [f for f in task.snapshot.get("xhs_files", []) if f["kind"] == "xhs_image"]
                    if page > len(pack["slides"]) or len(images) != len(pack["slides"]):
                        raise HTTPException(409, "单页重绘需要已有完整图片组")
                    if (
                        payload.get("theme", task.snapshot["task_input"]["theme"])
                        != task.snapshot["task_input"]["theme"]
                    ):
                        raise HTTPException(422, "更换主题需要整组重绘")
                    if payload.get("pack") and (
                        len(payload["pack"]["slides"]) != len(pack["slides"])
                        or any(
                            s != pack["slides"][i]
                            for i, s in enumerate(payload["pack"]["slides"])
                            if i != page - 1
                        )
                    ):
                        raise HTTPException(422, "单页重绘只能修改指定页，其他页修改请使用整组重绘")
            elif kind == "regenerate_topics":
                if task.status != "WAITING_TOPIC_SELECTION":
                    raise HTTPException(409, "只有等待选题时可以换一组选题")
            job = Job(
                task_id=task_id,
                kind=kind,
                request_key=key,
                request_hash=digest,
                payload={**payload, "expected_checkpoint": task.checkpoint_id}
                if kind != "retry"
                else payload,
            )
            session.add(job)
            task.status = "QUEUED"
            task.last_error = None
            session.add(Event(task_id=task_id, kind=f"{kind}_queued", detail=payload))
            await session.flush()
            return job

    @staticmethod
    def validate_interrupt(task: Task, interrupt_id: str, status: str):
        if task.status != status or task.interrupt_id != interrupt_id:
            raise HTTPException(409, "断点或阶段已更新，请刷新任务状态")
