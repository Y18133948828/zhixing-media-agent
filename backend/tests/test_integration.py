import asyncio
import io
import json
import zipfile
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.core.llm_factory import LLM, ModelOutputError
from app.main import create_app
from app.models import Job, Task

pytestmark = pytest.mark.integration


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "integration-test-api-key"},
    )


async def wait(client, task_id, status):
    for _ in range(300):
        response = await client.get(f"/api/v1/tasks/{task_id}")
        response.raise_for_status()
        task = response.json()
        if task["status"] == status:
            return task
        if task["status"] == "FAILED" and status != "FAILED":
            pytest.fail(task["last_error"])
        await asyncio.sleep(0.1)
    pytest.fail(f"任务等待 {status} 超时，当前 {task['status']}")


async def test_postgres_restart_review_concurrency_export_and_recovery(integration_settings):
    settings = integration_settings
    app = create_app(settings)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await client.get("/health/ready")).status_code == 200
        assert "/docs-assets/swagger-ui-bundle.js" in (await client.get("/docs")).text
        assert (await client.get("/docs-assets/swagger-ui.css")).status_code == 200
        assert (await client.get("/api/v1/tasks", headers={"X-API-Key": "bad"})).status_code == 401
        key = str(uuid4())
        response = await client.post(
            "/api/v1/tasks", headers={"Idempotency-Key": key}, json={"card_count": 5}
        )
        assert response.status_code == 202
        task_id = response.json()["id"]
        duplicate = await client.post(
            "/api/v1/tasks", headers={"Idempotency-Key": key}, json={"card_count": 5}
        )
        assert duplicate.json()["id"] == task_id
        assert (
            await client.post("/api/v1/tasks", headers={"Idempotency-Key": key}, json={"card_count": 3})
        ).status_code == 409
        task = await wait(client, task_id, "WAITING_TOPIC_SELECTION")
        interrupt_id = task["interrupt_id"]
        assert task["state"]["mode"] == "mock"

    # New DB pool, checkpointer, graph and FastAPI lifecycle: no in-memory state survives.
    app = create_app(settings)
    async with app.router.lifespan_context(app), client_for(app) as client:
        task = (await client.get(f"/api/v1/tasks/{task_id}")).json()
        assert task["interrupt_id"] == interrupt_id
        payload = {"interrupt_id": interrupt_id, "topic_id": "topic-1", "operator": "测试审核人"}
        responses = await asyncio.gather(
            *[client.post(f"/api/v1/tasks/{task_id}/select-topic", json=payload) for _ in range(2)]
        )
        assert sorted(r.status_code for r in responses) == [202, 409]
        task = await wait(client, task_id, "WAITING_ARTICLE_REVIEW")
        assert (await client.get(f"/api/v1/tasks/{task_id}/export")).status_code == 409
        old_id = task["interrupt_id"]
        reject = {
            "interrupt_id": old_id,
            "article_version": 1,
            "decision": "rejected",
            "feedback": "把开头改为初学者能理解的例子",
            "operator": "测试审核人",
        }
        assert (await client.post(f"/api/v1/tasks/{task_id}/review", json=reject)).status_code == 202
        for _ in range(100):
            task = await wait(client, task_id, "WAITING_ARTICLE_REVIEW")
            if task["state"]["article_version"] == 2:
                break
            await asyncio.sleep(0.1)
        assert task["state"]["article_version"] == 2
        assert (await client.post(f"/api/v1/tasks/{task_id}/review", json=reject)).status_code == 409
        approve = {
            "interrupt_id": task["interrupt_id"],
            "article_version": 2,
            "decision": "approved",
            "operator": "测试审核人",
        }
        idem = str(uuid4())
        accepted = await client.post(
            f"/api/v1/tasks/{task_id}/review", json=approve, headers={"Idempotency-Key": idem}
        )
        assert accepted.status_code == 202
        repeated = await client.post(
            f"/api/v1/tasks/{task_id}/review", json=approve, headers={"Idempotency-Key": idem}
        )
        assert repeated.json()["id"] == accepted.json()["id"]
        task = await wait(client, task_id, "READY")
        assets = (await client.get(f"/api/v1/tasks/{task_id}/assets")).json()
        assert len([a for a in assets if a["kind"] == "xhs_image"]) == 5
        download = await client.get(f"/api/v1/tasks/{task_id}/export")
        assert download.status_code == 200
        with zipfile.ZipFile(io.BytesIO(download.content)) as archive:
            assert len([n for n in archive.namelist() if n.endswith(".png")]) == 5
            assert json.loads(archive.read("manifest.json"))["article_version"] == 2
        assert len((await client.get(f"/api/v1/tasks/{task_id}/revisions")).json()) == 2
        assert (
            await client.put(
                f"/api/v1/tasks/{task_id}/feedback",
                json={
                    "platform": "xiaohongshu",
                    "views": 100,
                    "likes": 8,
                    "saves": 12,
                    "comments": 2,
                    "observed_at": "2026-10-28T10:00:00+08:00",
                },
            )
        ).status_code == 200
        # Simulate death after checkpoint completion but before job acknowledgement.
        await asyncio.sleep(0.2)
        async with app.state.db.sessions() as session, session.begin():
            stored_job = await session.get(Job, accepted.json()["id"])
            stored_job.status = "running"
            stored_task = await session.get(Task, task_id)
            stored_task.status = "RUNNING"
    app = create_app(settings)
    async with app.router.lifespan_context(app), client_for(app) as client:
        assert (await wait(client, task_id, "READY"))["state"]["article_version"] == 2
        async with app.state.db.sessions() as session:
            jobs = (await session.scalars(select(Job).where(Job.task_id == task_id))).all()
            assert all(j.status == "succeeded" for j in jobs)


async def test_failed_writer_retries_from_checkpoint(integration_settings, monkeypatch):
    original = LLM.generate
    failures = 0

    async def flaky(self, schema, *args, **kwargs):
        nonlocal failures
        if schema.__name__ == "Article" and failures == 0:
            failures += 1
            raise ModelOutputError("模拟模型暂时不可用")
        return await original(self, schema, *args, **kwargs)

    monkeypatch.setattr(LLM, "generate", flaky)
    app = create_app(integration_settings)
    async with app.router.lifespan_context(app), client_for(app) as client:
        response = await client.post("/api/v1/tasks", json={})
        task_id = response.json()["id"]
        task = await wait(client, task_id, "WAITING_TOPIC_SELECTION")
        await client.post(
            f"/api/v1/tasks/{task_id}/select-topic",
            json={"interrupt_id": task["interrupt_id"], "topic_id": "topic-1", "operator": "测试"},
        )
        assert (await wait(client, task_id, "FAILED"))["last_error"] == "模拟模型暂时不可用"
        assert (await client.post(f"/api/v1/tasks/{task_id}/retry")).status_code == 202
        task = await wait(client, task_id, "WAITING_ARTICLE_REVIEW")
        assert task["state"]["article_version"] == 1
