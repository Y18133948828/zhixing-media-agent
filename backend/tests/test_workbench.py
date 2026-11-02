import asyncio
import json
from uuid import uuid4

import httpx
import pytest

from app.main import create_app

pytestmark = pytest.mark.integration


async def wait(client, task_id, status):
    for _ in range(300):
        response = await client.get(f"/api/v1/tasks/{task_id}")
        assert response.status_code == 200
        task = response.json()
        if task["status"] == status:
            return task
        assert task["status"] != "FAILED", task["last_error"]
        await asyncio.sleep(0.1)
    pytest.fail(f"等待 {status} 超时")


async def test_jwt_isolation_sse_metrics_rerender_and_history_fork(integration_settings):
    app = create_app(integration_settings)
    async with app.router.lifespan_context(app):
        async with (
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as first,
            httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as second,
        ):
            for client in (first, second):
                credentials = {
                    "username": "editor_" + uuid4().hex[:10],
                    "password": "local-test-pass-2026",
                }
                registered = await client.post("/api/v1/auth/register", json=credentials)
                assert registered.status_code == 201
                login = await client.post("/api/v1/auth/login", json=credentials)
                assert login.status_code == 200
                client.headers["Authorization"] = "Bearer " + login.json()["access_token"]
                assert (await client.get("/api/v1/auth/me")).json()["username"] == credentials[
                    "username"
                ]
            key = str(uuid4())
            left = await first.post(
                "/api/v1/tasks", headers={"Idempotency-Key": key}, json={"card_count": 3}
            )
            right = await second.post(
                "/api/v1/tasks", headers={"Idempotency-Key": key}, json={"card_count": 3}
            )
            assert left.status_code == right.status_code == 202
            task_id = left.json()["id"]
            assert task_id != right.json()["id"]
            assert (await second.get(f"/api/v1/tasks/{task_id}")).status_code == 404
            assert (await second.get(f"/api/v1/tasks/{task_id}/stream")).status_code == 404
            await first.post(
                "/api/v1/knowledge",
                json={"category": "AI应用开发", "title": "专属资料", "body": "Python工作流课程"},
            )
            assert (await second.get("/api/v1/knowledge")).json() == []
            task = await wait(first, task_id, "WAITING_TOPIC_SELECTION")
            accepted = await first.post(
                f"/api/v1/tasks/{task_id}/select-topic",
                json={
                    "topic_id": "topic-1",
                    "interrupt_id": task["interrupt_id"],
                    "operator": "冒充其他人",
                },
            )
            assert accepted.status_code == 202
            assert (await second.get("/api/v1/jobs/" + accepted.json()["id"])).status_code == 404
            task = await wait(first, task_id, "WAITING_ARTICLE_REVIEW")
            events = await first.get(f"/api/v1/tasks/{task_id}/stream")
            frames = [
                json.loads(line[5:]) for line in events.text.splitlines() if line.startswith("data:")
            ]
            assert any(e["type"] == "article_delta" and e["data"]["source"] == "mock" for e in frames)
            assert frames[-1]["type"] == "done"
            measured = (await first.get(f"/api/v1/tasks/{task_id}/metrics")).json()
            assert measured["summary"]["calls"] == 2
            assert measured["summary"]["input_tokens"] is None
            assert measured["summary"]["estimated_cost"] is None
            assert task["state"]["selection_record"]["operator"] != "冒充其他人"
            await first.post(
                f"/api/v1/tasks/{task_id}/review",
                json={
                    "interrupt_id": task["interrupt_id"],
                    "article_version": 1,
                    "decision": "approved",
                    "operator": "测试",
                },
            )
            task = await wait(first, task_id, "READY")
            assets = (await first.get(f"/api/v1/tasks/{task_id}/assets")).json()
            assert (await second.get(assets[0]["download_url"])).status_code == 404
            old = task["checkpoint_id"]
            pack = task["state"]["xhs_pack"]
            pack["slides"][0]["title"] = "更新后的封面标题"
            # Preserve the edited pack and theme if rendering fails before the checkpoint is saved.
            from unittest.mock import patch

            from app.agents.tools.image_renderer.html_playwright import CardRenderer

            async def broken_render(*args, **kwargs):
                raise RuntimeError("temporary renderer failure")

            with patch.object(CardRenderer, "render", broken_render):
                failed = await first.post(
                    f"/api/v1/tasks/{task_id}/rerender", json={"pack": pack, "theme": "dark"}
                )
                assert failed.status_code == 202
                for _ in range(100):
                    current = (await first.get(f"/api/v1/tasks/{task_id}")).json()
                    if current["status"] == "FAILED":
                        break
                    await asyncio.sleep(0.05)
                assert current["status"] == "FAILED"
            assert (await first.post(f"/api/v1/tasks/{task_id}/retry")).status_code == 202
            task = await wait(first, task_id, "READY")
            assert task["state"]["xhs_pack"]["slides"][0]["title"] == "更新后的封面标题"
            assert task["state"]["task_input"]["theme"] == "dark"
            rendered = await first.post(
                f"/api/v1/tasks/{task_id}/rerender", json={"pack": pack, "theme": "dark"}
            )
            assert rendered.status_code == 202
            task = await wait(first, task_id, "READY")
            assert task["checkpoint_id"] != old
            assert task["state"]["xhs_pack"]["slides"][0]["title"] == "更新后的封面标题"
            assert task["state"]["task_input"]["theme"] == "dark"
            feedback = {
                "platform": "xiaohongshu",
                "views": 120,
                "likes": 8,
                "saves": 12,
                "comments": 2,
                "observed_at": "2026-10-28T16:00:00+08:00",
                "note": "案例类收藏较多",
            }
            assert (
                await first.put(f"/api/v1/tasks/{task_id}/feedback", json=feedback)
            ).status_code == 200
            assert (await second.get(f"/api/v1/tasks/{task_id}/feedback")).status_code == 404
            feedback["views"] = 2**31
            assert (
                await first.put(f"/api/v1/tasks/{task_id}/feedback", json=feedback)
            ).status_code == 422
            history = (await first.get(f"/api/v1/tasks/{task_id}/checkpoints")).json()
            checkpoint = next(
                h
                for h in history
                if h["has_article"] and not h["ready"] and h["next"] == ["review_article"]
            )
            forked = await first.post(
                f"/api/v1/tasks/{task_id}/fork", json={"checkpoint_id": checkpoint["id"]}
            )
            assert forked.status_code == 202
            new_id = forked.json()["id"]
            copied = await wait(first, new_id, "WAITING_ARTICLE_REVIEW")
            assert copied["thread_id"] != task["thread_id"]
            assert (await first.get(f"/api/v1/tasks/{task_id}")).json()["status"] == "READY"
            assert (
                await first.patch(
                    f"/api/v1/tasks/{task_id}", json={"title": "归档示例", "archived": True}
                )
            ).status_code == 200
            archived = (await first.get("/api/v1/tasks?archived=true")).json()["items"]
            assert any(t["id"] == task_id for t in archived)
