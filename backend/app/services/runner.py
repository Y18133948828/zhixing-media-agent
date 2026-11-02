import asyncio
import hashlib
import json
import logging

import psycopg
from langgraph.types import Command
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert

from app.agents.graph import build_graph
from app.core.content_safety import SafetyRejection
from app.core.database import Database
from app.core.llm_factory import LLM, ModelOutputError
from app.core.observability import correlation, trace_span
from app.core.telemetry import runtime
from app.models import Asset, CallMetric, Event, Job, Revision, SafetyAudit, Task
from app.services.storage import Storage

logger = logging.getLogger(__name__)


class Runner:
    """One in-process background consumer for the local demo, with durable PostgreSQL jobs."""

    def __init__(self, db: Database, llm: LLM, storage: Storage, checkpointer):
        self.db = db
        self.graph_llm = llm
        self.graph = build_graph(checkpointer, llm, storage)
        self.storage = storage

    async def loop(self):
        while True:
            try:
                processed = await self.run_once()
                if not processed:
                    from app.services.evaluation_service import EvaluationRunner

                    processed = await EvaluationRunner(self.db, self.graph_llm).run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # No exception message: DSNs or model URLs may contain credentials.
                logger.error("后台任务轮询失败：%s", type(exc).__name__)
                processed = False
            if not processed:
                await asyncio.sleep(self.db.settings.worker_poll_seconds)

    async def run_once(self):
        async with self.db.sessions() as session:
            jobs = (
                await session.execute(
                    select(Job.id, Job.task_id)
                    .where(Job.status.in_(["queued", "running"]))
                    .order_by(Job.created_at)
                    .limit(50)
                )
            ).all()
        for job_id, task_id in jobs:
            # Session-scoped advisory lock is released automatically after process death.
            # It prevents two local reload processes from advancing the same thread together.
            lock_id = int.from_bytes(hashlib.sha256(task_id.encode()).digest()[:8], "big", signed=True)
            async with await psycopg.AsyncConnection.connect(
                self.db.settings.database_url, autocommit=True, connect_timeout=5
            ) as conn:
                result = await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                if not (await result.fetchone())[0]:
                    continue
                try:
                    async with self.db.sessions() as session:
                        job = await session.get(Job, job_id)
                        if job.status not in ("queued", "running"):
                            continue
                        task = await session.get(Task, task_id)
                        job.status, task.status = "running", "RUNNING"
                        job.attempts += 1
                        await session.commit()
                    await self.execute(task, job)
                    return True
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        return False

    async def execute(self, task: Task, job: Job):
        async def emit_event(kind, detail):
            async with self.db.sessions() as session, session.begin():
                session.add(Event(task_id=task.id, kind=kind, detail={**detail, "job_id": job.id}))

        async def save_metric(data):
            async with self.db.sessions() as session, session.begin():
                session.add(CallMetric(task_id=task.id, job_id=job.id, **data))

        async def audit(data):
            async with self.db.sessions() as session, session.begin():
                session.add(SafetyAudit(task_id=task.id, **data))

        token = runtime.set(
            {"emit": emit_event, "record_call": save_metric, "audit": audit, "owner_id": task.owner_id}
        )
        log_token = correlation.set({"task_id": task.id, "job_id": job.id})
        try:
            async with trace_span(
                "media_workflow", self.db.settings, {"task_id": task.id, "kind": job.kind}
            ):
                await self._execute(task, job)
        finally:
            runtime.reset(token)
            correlation.reset(log_token)

    async def _execute(self, task: Task, job: Job):
        config = {"configurable": {"thread_id": task.thread_id}, "recursion_limit": 500}
        kind = job.payload.get("original_kind", job.kind)
        payload = job.payload.get("original_payload", job.payload)
        try:
            before = await self.graph.aget_state(config)
            has_interrupt = any(t.interrupts for t in before.tasks)
            current_checkpoint = (before.config or {}).get("configurable", {}).get("checkpoint_id")
            graph_input = None
            run = True
            if kind == "rerender":
                if before.values.get("last_render_job_id") != job.id:
                    await self.repair_images(task, job, before, config, payload)
                run = False
            elif kind == "fork" and not before.values:
                copied = payload["state"]
                copied.update(
                    task_id=task.id,
                    mode=self.db.settings.llm_provider,
                    ready=False,
                    xhs_files=[],
                    wechat_files=[],
                    last_render_job_id="",
                )
                if copied.get("approved_article"):
                    as_node = "review_article"
                elif copied.get("article_draft"):
                    as_node = "write_article"
                elif copied.get("selected_topic"):
                    as_node = (
                        "review_topic"
                        if copied.get("topic_review_status") == "approved"
                        else "select_topic"
                    )
                elif copied.get("candidate_topics"):
                    as_node = "topics"
                else:
                    as_node = None
                if as_node:
                    await self.graph.aupdate_state(config, copied, as_node=as_node)
                else:
                    graph_input = copied
            elif kind == "start" and not before.values:
                context = payload["context"]
                if self.db.settings.embedding_provider != "disabled" and not context.get(
                    "retrieval_complete"
                ):
                    from app.services.knowledge_service import KnowledgeService

                    async with asyncio.timeout(120):
                        found = await KnowledgeService(self.db).search(
                            task.owner_id,
                            task.input["course_category"],
                            task.input["brief"] + " " + task.input["target_audience"],
                        )
                    context = {
                        **context,
                        "knowledge": found["items"],
                        "retrieval": {"mode": found["mode"]},
                        "retrieval_complete": True,
                    }
                    async with self.db.sessions() as session, session.begin():
                        stored = await session.get(Job, job.id)
                        stored.payload = {**stored.payload, "context": context}
                graph_input = {
                    "task_id": task.id,
                    "task_input": task.input,
                    "context": context,
                    "mode": self.db.settings.llm_provider,
                }
            elif kind == "regenerate_topics" and current_checkpoint == payload.get(
                "expected_checkpoint"
            ):
                graph_input = {
                    "topic_round": before.values.get("topic_round", 0) + 1,
                    "context": {
                        **before.values.get("context", {}),
                        "previous_candidates": before.values.get("candidate_topics", []),
                    },
                }
            elif kind in ("select", "review", "topic_review") and current_checkpoint == payload.get(
                "expected_checkpoint"
            ):
                if not has_interrupt:
                    raise ValueError("断点已经发生变化")
                graph_input = Command(
                    resume={k: v for k, v in payload.items() if k != "expected_checkpoint"}
                )
            elif has_interrupt or not before.next:
                # A previous process already applied this command, but died before marking the job done.
                run = False
            if run:
                async with asyncio.timeout(self.db.settings.job_timeout_seconds):
                    async for update in self.graph.astream(graph_input, config, stream_mode="updates"):
                        nodes = [k for k in update if k != "__interrupt__"]
                        if nodes:
                            async with self.db.sessions() as session, session.begin():
                                session.add(
                                    Event(task_id=task.id, kind="graph_update", detail={"nodes": nodes})
                                )
            await self.project(task.id, config)
            async with self.db.sessions() as session, session.begin():
                stored = await session.get(Job, job.id)
                stored.status, stored.error = "succeeded", None
        except asyncio.CancelledError:
            # Leave the job running: startup recovers it using the persisted checkpoint.
            raise
        except Exception as exc:
            try:
                await self.project(task.id, config)
            except Exception:
                pass
            if isinstance(exc, (ModelOutputError, SafetyRejection)):
                message = str(exc)
            elif isinstance(exc, TimeoutError):
                message = "操作超时，可重试后从断点继续"
            else:
                message = f"任务执行失败（{type(exc).__name__}），请检查模型输出或本地渲染环境"
            logger.error("任务 %s 失败：%s", task.id, type(exc).__name__)
            async with self.db.sessions() as session, session.begin():
                stored_task = await session.get(Task, task.id)
                stored_job = await session.get(Job, job.id)
                stored_task.status, stored_task.last_error = "FAILED", message
                stored_job.status, stored_job.error = "failed", message
                session.add(
                    Event(task_id=task.id, kind="failed", detail={"job_id": job.id, "error": message})
                )

    async def repair_images(self, task, job, snapshot, config, payload):
        from app.services.image_engine import image_engine
        from app.services.wechat_format import format_wechat, reference_sources

        state = dict(snapshot.values)
        pack = payload.get("pack") or state["xhs_pack"]
        theme = payload.get("theme") or state["task_input"]["theme"]
        # A new render generation keeps historical files intact.
        version = f"{state['article_version']}-{job.id[:8]}"
        renderer = image_engine(self.db.settings, self.storage)
        page = payload.get("page")
        if page:
            files = await renderer.render(
                task.id, version, pack, theme, state["mode"], page_numbers=[page]
            )
            preserved = [
                f
                for f in state["xhs_files"]
                if f["kind"] == "xhs_image" and not f["path"].endswith(f"/card-{page:02}.png")
            ]
            files = sorted([*preserved, *files], key=lambda f: f["path"].rsplit("/", 1)[-1])
        else:
            files = await renderer.render(task.id, version, pack, theme, state["mode"])
        prefix = f"{task.id}/v{version}"
        files.append(
            self.storage.write(
                f"{prefix}/xiaohongshu.json",
                json.dumps(pack, ensure_ascii=False, indent=2),
                "xhs_metadata",
                "application/json",
            )
        )
        article = state["approved_article"]
        html = format_wechat(article, reference_sources(state))
        wechat = []
        if state["task_input"].get("workflow_template") != "xhs_only":
            wechat = [
                self.storage.write(
                    f"{prefix}/wechat.html",
                    '<!doctype html><meta charset="utf-8">' + html,
                    "wechat_html",
                    "text/html",
                ),
                self.storage.write(
                    f"{prefix}/article.md",
                    f"# {article['title']}\n\n{article['markdown']}",
                    "article_markdown",
                    "text/markdown",
                ),
            ]
        else:
            html = ""
        await self.graph.aupdate_state(
            config,
            {
                "xhs_pack": pack,
                "xhs_files": files,
                "wechat_files": wechat,
                "wechat_html_content": html,
                "ready": True,
                "last_render_job_id": job.id,
                "task_input": {**state["task_input"], "theme": theme},
            },
            as_node="ready",
        )

    async def project(self, task_id: str, config: dict):
        snapshot = await self.graph.aget_state(config)
        state = dict(snapshot.values)
        interrupts = [i for task in snapshot.tasks for i in task.interrupts]
        if interrupts:
            status = {
                "select_topic": "WAITING_TOPIC_SELECTION",
                "review_article": "WAITING_ARTICLE_REVIEW",
                "review_topic": "WAITING_TOPIC_REVIEW",
            }[interrupts[0].value["type"]]
        elif state.get("ready") and not snapshot.next:
            status = "READY"
        else:
            status = "RUNNING"
        async with self.db.sessions() as session, session.begin():
            task = await session.get(Task, task_id)
            task.snapshot = state
            if interrupts and interrupts[0].value["type"] in {"review_article", "review_topic"}:
                task.snapshot = {**state, "pending_review": interrupts[0].value}
            task.status = status
            task.checkpoint_id = (snapshot.config or {}).get("configurable", {}).get("checkpoint_id")
            task.interrupt_id = interrupts[0].id if interrupts else None
            task.last_error = None
            decisions = {r["article_version"]: r for r in state.get("review_history", [])}
            for revision in state.get("revisions", []):
                record = {
                    "task_id": task_id,
                    "version": revision["version"],
                    "article": revision["article"],
                    "decision": decisions.get(revision["version"]),
                }
                await session.execute(
                    insert(Revision)
                    .values(**record)
                    .on_conflict_do_update(
                        index_elements=[Revision.task_id, Revision.version],
                        set_={"decision": record["decision"]},
                    )
                )
            if status == "READY":
                # Asset identities remain stable on recovery, including when a response was lost.
                files = [*state.get("xhs_files", []), *state.get("wechat_files", [])]
                for file in files:
                    await session.execute(
                        insert(Asset)
                        .values(task_id=task_id, **file)
                        .on_conflict_do_update(
                            index_elements=[Asset.task_id, Asset.path], set_={"sha256": file["sha256"]}
                        )
                    )
                await session.execute(
                    delete(Asset).where(
                        Asset.task_id == task_id, Asset.path.not_in([f["path"] for f in files])
                    )
                )
            session.add(Event(task_id=task_id, kind="state_changed", detail={"status": status}))
