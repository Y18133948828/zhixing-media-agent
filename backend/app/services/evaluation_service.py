import asyncio
import hashlib
import logging
import time

import psycopg
from sqlalchemy import select

from app.core.telemetry import runtime
from app.models import CallMetric, EvaluationRun
from app.schemas.content import Article


def evaluate_rules(article, rules):
    text = article["title"] + "\n" + article["markdown"]
    checks = {
        "length": rules.get("min_chars", 0) <= len(article["markdown"]) <= rules.get("max_chars", 15000)
    }
    checks.update(
        {"include:" + t: t.casefold() in text.casefold() for t in rules.get("must_include", [])}
    )
    checks.update(
        {"avoid:" + t: t.casefold() not in text.casefold() for t in rules.get("must_avoid", [])}
    )
    return {
        "checks": checks,
        "passed": all(checks.values()),
        "score": sum(checks.values()) / len(checks),
        "scope": "规则符合度；事实准确性、教学质量仍需人工评价",
    }


class EvaluationRunner:
    def __init__(self, db, llm):
        self.db, self.llm = db, llm

    async def run_once(self):
        async with self.db.sessions() as session:
            ids = (
                await session.scalars(
                    select(EvaluationRun.id)
                    .where(EvaluationRun.status.in_(["queued", "running"]))
                    .order_by(EvaluationRun.created_at)
                    .limit(10)
                )
            ).all()
        for run_id in ids:
            lock_id = int.from_bytes(
                hashlib.sha256(("evaluation:" + run_id).encode()).digest()[:8], "big", signed=True
            )
            async with await psycopg.AsyncConnection.connect(
                self.db.settings.database_url, autocommit=True, connect_timeout=5
            ) as conn:
                cursor = await conn.execute("SELECT pg_try_advisory_lock(%s)", (lock_id,))
                if not (await cursor.fetchone())[0]:
                    continue
                try:
                    await self.execute(run_id)
                    return True
                finally:
                    await conn.execute("SELECT pg_advisory_unlock(%s)", (lock_id,))
        return False

    async def execute(self, run_id):
        async with self.db.sessions() as session, session.begin():
            run = await session.get(EvaluationRun, run_id)
            if run.status not in {"queued", "running"}:
                return
            run.status = "running"
        completed = {(r["case_id"], r["variant"]) for r in run.results}
        try:
            async with asyncio.timeout(self.db.settings.job_timeout_seconds * 2):
                for case in run.cases:
                    for variant in run.variants:
                        identity = (case["id"], variant["digest"])
                        if identity in completed:
                            continue
                        calls = []

                        async def record(data):
                            calls.append(data)
                            async with self.db.sessions() as session, session.begin():
                                session.add(
                                    CallMetric(
                                        task_id=case["task_id"],
                                        job_id=run_id,
                                        **{
                                            **data,
                                            "details": {
                                                **data.get("details", {}),
                                                "evaluation_run": run_id,
                                            },
                                        },
                                    )
                                )

                        token = runtime.set({"owner_id": run.owner_id, "record_call": record})
                        tick = time.monotonic()
                        try:
                            article = await self.llm.generate(
                                Article,
                                variant["instruction"],
                                case["context"],
                                options={**variant["options"], "prompt_digest": variant["digest"]},
                            )
                        finally:
                            runtime.reset(token)
                        data = {
                            "case_id": case["id"],
                            "name": case["name"],
                            "variant": variant["digest"],
                            "variant_name": variant["name"],
                            "article": article.model_dump(mode="json"),
                            "expected": case["expected"],
                            **evaluate_rules(article.model_dump(mode="json"), case["rules"]),
                            "duration_ms": round((time.monotonic() - tick) * 1000, 2),
                            "calls": calls,
                        }
                        async with self.db.sessions() as session, session.begin():
                            current = await session.get(EvaluationRun, run_id)
                            current.results = [*current.results, data]
            async with self.db.sessions() as session, session.begin():
                current = await session.get(EvaluationRun, run_id)
                current.status = "succeeded"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger(__name__).error("评测失败：%s", type(exc).__name__)
            async with self.db.sessions() as session, session.begin():
                current = await session.get(EvaluationRun, run_id)
                current.status, current.error = (
                    "failed",
                    "评测执行失败；已完成样本保留，请检查模型配置后重新提交",
                )
