import asyncio
import json
import zipfile
from io import BytesIO
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import select

from app.main import create_app
from app.models import KnowledgeChunk, User

pytestmark = pytest.mark.integration


@pytest.fixture
async def workbench(integration_settings, tmp_path):
    settings = integration_settings.model_copy(update={"log_dir": tmp_path / "logs"})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            credentials = {"username": "opt_" + uuid4().hex[:12], "password": "test-local-password-2026"}
            registered = await client.post("/api/v1/auth/register", json=credentials)
            assert registered.status_code == 201
            data = registered.json()
            client.headers["Authorization"] = "Bearer " + data["access_token"]
            yield app, client, data["user"], credentials


async def wait_task(client, task_id, status, review_index=None):
    for _ in range(400):
        result = await client.get(f"/api/v1/tasks/{task_id}")
        assert result.status_code == 200, result.text
        task = result.json()
        if task["status"] == status and (
            review_index is None
            or task["state"].get("pending_review", {}).get("review_index") == review_index
        ):
            return task
        assert task["status"] != "FAILED", task["last_error"]
        await asyncio.sleep(0.05)
    pytest.fail("工作流等待超时")


async def select_first(client, task):
    result = await client.post(
        f"/api/v1/tasks/{task['id']}/select-topic",
        json={
            "interrupt_id": task["interrupt_id"],
            "topic_id": task["state"]["candidate_topics"][0]["id"],
            "operator": "client-provided",
        },
    )
    assert result.status_code == 202, result.text
    return await wait_task(client, task["id"], "WAITING_ARTICLE_REVIEW")


async def approve(client, task):
    return await client.post(
        f"/api/v1/tasks/{task['id']}/review",
        json={
            "interrupt_id": task["interrupt_id"],
            "article_version": task["state"]["article_version"],
            "decision": "approved",
            "operator": "client-provided",
        },
    )


async def create_ready(client, **overrides):
    result = await client.post("/api/v1/tasks", json={"card_count": 3, **overrides})
    assert result.status_code == 202, result.text
    task = await wait_task(client, result.json()["id"], "WAITING_TOPIC_SELECTION")
    task = await select_first(client, task)
    assert (await approve(client, task)).status_code == 202
    return await wait_task(client, task["id"], "READY")


async def test_prompt_snapshot_experiment_rating_and_reproducible_evaluation(workbench):
    app, client, user, _ = workbench
    builtin = (await client.get("/api/v1/prompts")).json()["builtins"]
    instruction = next(v["instruction"] for v in builtin if v["stage"] == "Article")
    versions = []
    for name, active in [("A", True), ("B", False)]:
        response = await client.post(
            "/api/v1/prompts",
            json={
                "stage": "Article",
                "name": name,
                "instruction": instruction + "\n版本" + name,
                "options": {"temperature": 0.3, "max_tokens": 2000},
                "activate": active,
            },
        )
        assert response.status_code == 201
        versions.append(response.json())
    task = await create_ready(client, workflow_template="wechat_only")
    original_id = task["input"]["prompt_snapshots"]["Article"]["version_id"]
    assert original_id == versions[0]["id"]
    assert task["state"]["xhs_files"] == []
    assert (await client.post(f"/api/v1/prompts/{versions[1]['id']}/activate")).status_code == 200
    assert (await client.get(f"/api/v1/tasks/{task['id']}")).json()["input"]["prompt_snapshots"][
        "Article"
    ]["version_id"] == original_id
    experiment = {
        "stage": "Article",
        "control_id": versions[0]["id"],
        "treatment_id": versions[1]["id"],
        "percentage": 100,
        "enabled": True,
    }
    assert (await client.put("/api/v1/prompt-experiments", json=experiment)).status_code == 200
    key = uuid4().hex
    first = await client.post("/api/v1/tasks", json={}, headers={"Idempotency-Key": key})
    assert first.json()["input"]["prompt_snapshots"]["Article"]["variant"] == "B"
    experiment["percentage"] = 0
    await client.put("/api/v1/prompt-experiments", json=experiment)
    repeated = await client.post("/api/v1/tasks", json={}, headers={"Idempotency-Key": key})
    assert repeated.json()["id"] == first.json()["id"]
    assert repeated.json()["input"]["prompt_snapshots"]["Article"]["variant"] == "B"
    assert (
        await client.put(
            f"/api/v1/tasks/{task['id']}/rating",
            json={"score": 5, "dimensions": {"accuracy": 4}, "comment": "示例评价"},
        )
    ).status_code == 200
    assert (await client.get(f"/api/v1/tasks/{task['id']}/rating")).json()["score"] == 5
    case = await client.post(
        "/api/v1/evaluation-cases",
        json={
            "task_id": task["id"],
            "name": "固定输入",
            "rules": {"must_include": ["人工审核"], "must_avoid": ["保证就业"]},
        },
    )
    assert case.status_code == 201
    run = await client.post(
        "/api/v1/evaluations",
        json={"case_ids": [case.json()["id"]], "prompt_ids": [v["id"] for v in versions]},
    )
    assert run.status_code == 202 and run.json()["planned_generations"] == 2
    for _ in range(200):
        runs = (await client.get("/api/v1/evaluations")).json()
        current = next(r for r in runs if r["id"] == run.json()["id"])
        if current["status"] in {"succeeded", "failed"}:
            break
        await asyncio.sleep(0.05)
    assert current["status"] == "succeeded", current
    assert len(current["results"]) == 2
    assert all(r["passed"] and r["score"] == 1 for r in current["results"])
    assert current["results"][0]["expected"] == task["state"]["approved_article"]
    assert (await client.get(f"/api/v1/tasks/{task['id']}")).json()["status"] == "READY"
    overview = (await client.get("/api/v1/dashboard")).json()
    assert overview["average_rating"] == 5 and overview["rating_count"] == 1
    assert overview["funnel"]["ready"] == 1
    next_task = (await client.post("/api/v1/tasks", json={})).json()
    next_task = await wait_task(client, next_task["id"], "WAITING_TOPIC_SELECTION")
    assert next_task["state"]["context"]["quality_feedback"][0]["score"] == 5
    # No authenticated namespace may choose another user's prompts or samples.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as stranger:
        result = await stranger.post(
            "/api/v1/auth/register",
            json={"username": "other_" + uuid4().hex[:12], "password": "other-password-2026"},
        )
        stranger.headers["Authorization"] = "Bearer " + result.json()["access_token"]
        assert (await stranger.post(f"/api/v1/prompts/{original_id}/activate")).status_code == 404
        assert (
            await stranger.post("/api/v1/evaluations", json={"case_ids": [case.json()["id"]]})
        ).status_code == 404


@pytest.mark.parametrize("template", ["xhs_wechat", "xhs_only"])
async def test_single_page_render_preserves_other_pages_and_export(workbench, template):
    _, client, _, _ = workbench
    task = await create_ready(client, workflow_template=template)
    before = {
        r["filename"]: r
        for r in (await client.get(f"/api/v1/tasks/{task['id']}/assets")).json()
        if r["kind"] == "xhs_image"
    }
    pack = task["state"]["xhs_pack"]
    pack["slides"][1]["title"] = "仅修改第二页"
    result = await client.post(f"/api/v1/tasks/{task['id']}/rerender", json={"page": 2, "pack": pack})
    assert result.status_code == 202
    task = await wait_task(client, task["id"], "READY")
    after = {
        r["filename"]: r
        for r in (await client.get(f"/api/v1/tasks/{task['id']}/assets")).json()
        if r["kind"] == "xhs_image"
    }
    assert len(after) == 3
    if template == "xhs_only":
        assert task["state"]["wechat_files"] == [] and task["state"]["wechat_html_content"] == ""
    for filename in ["card-01.png", "card-03.png"]:
        assert after[filename]["id"] == before[filename]["id"]
        assert after[filename]["sha256"] == before[filename]["sha256"]
    assert after["card-02.png"]["sha256"] != before["card-02.png"]["sha256"]
    data = await client.get(f"/api/v1/tasks/{task['id']}/export")
    with zipfile.ZipFile(BytesIO(data.content)) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist()))
        assert len([name for name in archive.namelist() if name.endswith(".png")]) == 3
    pack["slides"][0]["title"] = "修改了别的页"
    assert (
        await client.post(f"/api/v1/tasks/{task['id']}/rerender", json={"page": 2, "pack": pack})
    ).status_code == 422


async def test_multilevel_review_roles_assignment_and_regeneration(workbench):
    app, owner, user, _ = workbench
    clients = []
    try:
        ids = []
        for role in ["manager", "director"]:
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
            clients.append(client)
            response = await client.post(
                "/api/v1/auth/register",
                json={"username": role + uuid4().hex[:10], "password": "reviewer-password-2026"},
            )
            ids.append(response.json()["user"]["id"])
            client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
            async with app.state.db.sessions() as session, session.begin():
                row = await session.get(User, ids[-1])
                row.role = role
        result = await owner.post(
            "/api/v1/tasks", json={"reviewer_ids": ids, "workflow_template": "wechat_only"}
        )
        assert result.status_code == 202
        task = await wait_task(owner, result.json()["id"], "WAITING_TOPIC_SELECTION")
        old_titles = [r["title"] for r in task["state"]["candidate_topics"]]
        assert (await owner.post(f"/api/v1/tasks/{task['id']}/regenerate-topics")).status_code == 202
        task = await wait_task(owner, task["id"], "WAITING_TOPIC_SELECTION")
        assert task["state"]["topic_round"] == 1
        assert [r["title"] for r in task["state"]["candidate_topics"]] != old_titles
        task = await select_first(owner, task)
        assert (await approve(clients[0], task)).status_code == 403
        assert (await approve(owner, task)).status_code == 202
        task = await wait_task(owner, task["id"], "WAITING_ARTICLE_REVIEW", 1)
        assert task["state"]["pending_review"]["reviewer"]["reviewer_id"] == ids[0]
        assert (await approve(owner, task)).status_code == 403
        assert (
            await clients[0].patch(f"/api/v1/tasks/{task['id']}", json={"title": "越权修改"})
        ).status_code == 404
        assert (await approve(clients[0], task)).status_code == 202
        task = await wait_task(owner, task["id"], "WAITING_ARTICLE_REVIEW", 2)
        assert (await approve(clients[1], task)).status_code == 202
        task = await wait_task(owner, task["id"], "READY")
        assert len(task["state"]["review_history"]) == 3
        assert task["state"]["review_history"][0]["operator"] == user["username"]
        listed = (await clients[1].get("/api/v1/tasks")).json()["items"]
        assert any(r["id"] == task["id"] for r in listed)
        assert (
            await clients[1].put(f"/api/v1/tasks/{task['id']}/rating", json={"score": 5})
        ).status_code == 404
    finally:
        for client in clients:
            await client.aclose()


async def test_vector_chunks_search_owner_isolation_and_deletion(workbench, monkeypatch):
    app, client, user, _ = workbench
    from app.services.knowledge_service import KnowledgeService

    async def embeddings(self, texts):
        return [[1.0, 0.0] if "状态" in t or "接力棒" in t else [0.0, 1.0] for t in texts]

    monkeypatch.setattr(KnowledgeService, "embed", embeddings)
    app.state.settings.embedding_provider = "openai_compatible"
    app.state.settings.embedding_model = "test-semantic-fixture"
    ids = []
    for title, body in [
        ("状态流转", "状态像接力棒，节点传递已经保存的信息。"),
        ("其他课程", "花卉种植步骤与水分管理。"),
    ]:
        response = await client.post(
            "/api/v1/knowledge", json={"title": title, "body": body, "category": "AI应用开发"}
        )
        ids.append(response.json()["id"])
        assert (await client.post(f"/api/v1/knowledge/{ids[-1]}/index")).json()["mode"] == "vector"
    found = (
        await client.get(
            "/api/v1/knowledge/search", params={"category": "AI应用开发", "query": "接力棒"}
        )
    ).json()
    assert found["mode"] == "vector_hybrid"
    assert found["items"][0]["title"] == "状态流转"
    async with app.state.db.sessions() as session:
        row = await session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == ids[0]))
        assert row.embedding == [1.0, 0.0] and row.owner_id == user["id"]
    assert (await client.delete(f"/api/v1/knowledge/{ids[0]}")).status_code == 200
    async with app.state.db.sessions() as session:
        assert (
            await session.scalar(select(KnowledgeChunk).where(KnowledgeChunk.knowledge_id == ids[0]))
            is None
        )


async def test_password_logout_revocation_and_validation_do_not_echo_password(workbench):
    _, client, _, credentials = workbench
    old_token = client.headers["Authorization"]
    invalid = await client.post(
        "/api/v1/auth/password", json={"old_password": credentials["password"], "new_password": "secret"}
    )
    assert invalid.status_code == 422 and "secret" not in invalid.text
    new_password = "replacement-password-2026"
    changed = await client.post(
        "/api/v1/auth/password",
        json={"old_password": credentials["password"], "new_password": new_password},
    )
    assert changed.status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    client.headers["Authorization"] = "Bearer " + changed.json()["access_token"]
    assert (await client.get("/api/v1/auth/me")).status_code == 200
    assert (await client.post("/api/v1/auth/login", json=credentials)).status_code == 401
    assert (await client.post("/api/v1/auth/logout")).status_code == 200
    assert (await client.get("/api/v1/auth/me")).status_code == 401
    client.headers["Authorization"] = old_token
    assert (await client.get("/api/v1/auth/me")).status_code == 401


async def test_topic_review_rejection_replans_and_role_downgrade_blocks_approval(workbench):
    app, owner, _, _ = workbench
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as reviewer:
        response = await reviewer.post(
            "/api/v1/auth/register",
            json={"username": "topic_manager_" + uuid4().hex[:8], "password": "topic-manager-password"},
        )
        reviewer_id = response.json()["user"]["id"]
        reviewer.headers["Authorization"] = "Bearer " + response.json()["access_token"]

        async def set_role(role):
            async with app.state.db.sessions() as session, session.begin():
                row = await session.get(User, reviewer_id)
                row.role = role

        await set_role("manager")
        result = await owner.post(
            "/api/v1/tasks",
            json={
                "reviewer_ids": [reviewer_id],
                "review_topics": True,
                "workflow_template": "wechat_only",
            },
        )
        task = await wait_task(owner, result.json()["id"], "WAITING_TOPIC_SELECTION")

        async def choose(task):
            response = await owner.post(
                f"/api/v1/tasks/{task['id']}/select-topic",
                json={"interrupt_id": task["interrupt_id"], "topic_id": "topic-1", "operator": "editor"},
            )
            assert response.status_code == 202
            return await wait_task(owner, task["id"], "WAITING_TOPIC_REVIEW")

        task = await choose(task)
        body = {
            "interrupt_id": task["interrupt_id"],
            "topic_round": 0,
            "decision": "rejected",
            "feedback": "换一个更具体的切口",
            "operator": "manager",
        }
        assert (
            await owner.post(f"/api/v1/tasks/{task['id']}/review-topic", json=body)
        ).status_code == 403
        assert (
            await reviewer.post(f"/api/v1/tasks/{task['id']}/review-topic", json=body)
        ).status_code == 202
        task = await wait_task(owner, task["id"], "WAITING_TOPIC_SELECTION")
        assert task["state"]["topic_round"] == 1 and len(task["state"]["topic_review_history"]) == 1
        task = await choose(task)
        body.update(interrupt_id=task["interrupt_id"], topic_round=1, decision="approved", feedback="")
        await set_role("editor")
        assert (
            await reviewer.post(f"/api/v1/tasks/{task['id']}/review-topic", json=body)
        ).status_code == 403
        await set_role("manager")
        assert (
            await reviewer.post(f"/api/v1/tasks/{task['id']}/review-topic", json=body)
        ).status_code == 202
        task = await wait_task(owner, task["id"], "WAITING_ARTICLE_REVIEW", 0)
        assert (await approve(owner, task)).status_code == 202
        task = await wait_task(owner, task["id"], "WAITING_ARTICLE_REVIEW", 1)
        assert (await approve(reviewer, task)).status_code == 202
        assert (await wait_task(owner, task["id"], "READY"))["state"][
            "topic_review_status"
        ] == "approved"


async def test_rate_limit_request_id_and_structured_log(integration_settings, tmp_path):
    settings = integration_settings.model_copy(update={"login_limit_per_minute": 2, "log_dir": tmp_path})
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app, client=("rate-" + uuid4().hex, 1000)),
            base_url="http://test",
        ) as client:
            for _ in range(2):
                response = await client.get(
                    "/api/v1/auth/sso/config", headers={"X-Request-ID": "correlation-test-2026"}
                )
                assert response.status_code == 200
                assert response.headers["X-Request-ID"] == "correlation-test-2026"
            response = await client.get("/api/v1/auth/sso/config")
            assert response.status_code == 429 and int(response.headers["Retry-After"]) > 0
    records = [
        json.loads(line) for line in (tmp_path / "media.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert any(r.get("request_id") == "correlation-test-2026" for r in records)


async def test_failed_reindex_does_not_commit_new_body_with_old_vectors(workbench, monkeypatch):
    app, client, _, _ = workbench
    body = {"category": "AI应用开发", "title": "原始资料", "body": "已验证的原始正文。"}
    result = await client.post("/api/v1/knowledge", json=body)
    knowledge_id = result.json()["id"]
    await client.post(f"/api/v1/knowledge/{knowledge_id}/index")
    from fastapi import HTTPException

    from app.services.knowledge_service import KnowledgeService

    async def broken(*args):
        raise HTTPException(502, "Embedding temporarily unavailable")

    monkeypatch.setattr(KnowledgeService, "embed", broken)
    app.state.settings.embedding_provider = "openai_compatible"
    changed = await client.put(
        f"/api/v1/knowledge/{knowledge_id}", json={**body, "body": "尚未完成索引的新正文。"}
    )
    assert changed.status_code == 502
    items = (await client.get("/api/v1/knowledge")).json()
    assert next(r for r in items if r["id"] == knowledge_id)["body"] == body["body"]


async def test_admin_roles_cannot_be_self_assigned_and_disable_revokes_sessions(workbench):
    app, client, user, _ = workbench
    assert (await client.get("/api/v1/admin/users")).status_code == 403
    assert (
        await client.patch(f"/api/v1/admin/users/{user['id']}", json={"role": "admin"})
    ).status_code == 403
    async with app.state.db.sessions() as session, session.begin():
        row = await session.get(User, user["id"])
        row.role = "admin"  # Represents local bootstrap, not an HTTP privilege escalation.
    assert (await client.get("/api/v1/admin/users")).status_code == 200
    assert (
        await client.patch(f"/api/v1/admin/users/{user['id']}", json={"role": "editor"})
    ).status_code == 409
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as other:
        result = await other.post(
            "/api/v1/auth/register",
            json={"username": "disable_" + uuid4().hex[:8], "password": "other-password-2026"},
        )
        other.headers["Authorization"] = "Bearer " + result.json()["access_token"]
        target = result.json()["user"]["id"]
        assert (
            await client.patch(
                f"/api/v1/admin/users/{target}", json={"role": "editor", "disabled": True}
            )
        ).status_code == 200
        assert (await other.get("/api/v1/auth/me")).status_code == 401
