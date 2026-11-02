import asyncio
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from app.agents.mock_model import mock_output
from app.core.config import BACKEND_ROOT, Settings
from app.core.content_safety import SafetyRejection, check_content
from app.core.llm_factory import LLM
from app.core.observability import correlation, trace_span
from app.core.resilience import CircuitBreaker, CircuitOpen
from app.core.telemetry import runtime
from app.main import create_app
from app.schemas.content import Article, TaskCreate, TopicBatch
from app.services.knowledge_service import KnowledgeService


async def test_breaker_half_open_is_single_probe_and_cancel_releases_it():
    breaker = CircuitBreaker(threshold=1)
    await breaker.acquire()
    await breaker.failure()
    with pytest.raises(CircuitOpen):
        await breaker.acquire()
    breaker.open_until = time.monotonic() - 1
    await breaker.acquire()
    with pytest.raises(CircuitOpen):
        await breaker.acquire()
    await breaker.cancelled()
    await breaker.acquire()
    await breaker.success()
    await breaker.acquire()
    assert breaker.failures == 0 and breaker.open_until == 0


async def test_stage_routing_cache_and_configured_fallback_collect_actual_calls():
    requests, calls = [], []
    article = mock_output("Article", {"selected_topic": {"title": "技术文章"}})

    async def collect(data):
        calls.append(data)

    token = runtime.set({"owner_id": "test-namespace", "record_call": collect})

    def handler(request):
        body = json.loads(request.content)
        requests.append((request.url.host, body))
        if request.url.host == "primary.example":
            return httpx.Response(503)
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(article)}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 30},
            },
        )

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://primary.example/v1",
        llm_api_key="primary-test-key",
        llm_model="big",
        llm_streaming=False,
        llm_attempts=1,
        llm_fallback_provider="openai_compatible",
        llm_fallback_base_url="https://fallback.example/v1",
        llm_fallback_api_key="fallback-test-key",
        llm_fallback_model="alternate",
        llm_cache_seconds=60,
    )
    llm = LLM(settings, httpx.MockTransport(handler))
    try:
        context = {"selected_topic": {"title": "技术文章"}, "task_input": {}}
        result = await llm.generate(
            Article, "按要求写文章", context, options={"temperature": 0.3, "max_tokens": 1200}
        )
        assert result.title == "技术文章"
        assert requests[0][1]["model"] == "big" and requests[1][1]["model"] == "alternate"
        assert requests[1][1]["temperature"] == 0.3 and requests[1][1]["max_tokens"] == 1200
        await llm.generate(
            Article, "按要求写文章", context, options={"temperature": 0.3, "max_tokens": 1200}
        )
        assert len(requests) == 2
        assert calls[0]["success"] is False and calls[1]["success"] is True
        assert calls[-1]["usage_source"] == "cache" and calls[-1]["cost"] == 0
        assert calls[-1]["model"] == "alternate"
    finally:
        runtime.reset(token)
        await llm.close()


async def test_provider_rejection_does_not_trigger_fallback():
    from app.core.llm_factory import ModelOutputError

    requests = []

    def reject(request):
        requests.append(request.url.host)
        return httpx.Response(400, json={"error": {"code": "content_filter"}})

    settings = Settings(
        _env_file=None,
        llm_provider="openai_compatible",
        llm_base_url="https://primary.example/v1",
        llm_api_key="test-key",
        llm_fallback_provider="openai_compatible",
        llm_fallback_base_url="https://alternate.example/v1",
        llm_fallback_api_key="test-alternate-key",
        llm_fallback_model="alternate-model",
    )
    llm = LLM(settings, httpx.MockTransport(reject))
    try:
        with pytest.raises(ModelOutputError):
            await llm.generate(Article, "技术文章", {"selected_topic": {"title": "测试"}})
        assert requests == ["primary.example"]
    finally:
        await llm.close()


async def test_adaptive_small_model_uses_real_usage_not_guessed_tokens():
    seen = []
    context = {"task_input": TaskCreate().model_dump()}

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": json.dumps(mock_output("TopicBatch", context))}}],
                "usage": {"prompt_tokens": 30, "completion_tokens": 80},
            },
        )

    llm = LLM(
        Settings(
            _env_file=None,
            llm_provider="openai_compatible",
            llm_api_key="test",
            llm_model="large",
            llm_small_model="small",
            routing_strategy="adaptive",
            llm_streaming=False,
        ),
        httpx.MockTransport(handler),
    )
    try:
        result = await llm.generate(TopicBatch, "生成选题", context)
        assert len(result.topics) == 4 and seen[0]["model"] == "small"
        assert seen[0]["temperature"] == 0.8
    finally:
        await llm.close()


async def test_safety_gateway_refusal_and_audit_never_store_content():
    audits = []

    async def audit(data):
        audits.append(data)

    token = runtime.set({"audit": audit})
    try:
        settings = Settings(_env_file=None, content_blocked_terms=["不允许的词"])
        with pytest.raises(SafetyRejection):
            await check_content(settings, "含有不允许的词及其他私密正文", "Article:input")
        assert audits[0]["verdict"] == "blocked"
        assert "私密正文" not in json.dumps(audits)
        settings = Settings(
            _env_file=None,
            content_review_url="https://review.example/check",
            content_review_key="review-key",
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"allowed": False}))
        ) as client:
            with pytest.raises(SafetyRejection):
                await check_content(settings, "待审内容", "image:input", client)
    finally:
        runtime.reset(token)


async def test_embeddings_protocol_rejects_missing_vectors_and_preserves_order():
    class DB:
        settings = Settings(
            _env_file=None,
            embedding_provider="openai_compatible",
            embedding_base_url="https://embedding.example/v1",
            embedding_api_key="embedding-key",
            embedding_model="embedding-test",
        )

    transport = httpx.MockTransport(
        lambda req: httpx.Response(
            200, json={"data": [{"index": 1, "embedding": [0, 1]}, {"index": 0, "embedding": [1, 0]}]}
        )
    )
    result = await KnowledgeService(DB(), transport).embed(["第一段", "第二段"])
    assert result == [[1, 0], [0, 1]]
    bad = httpx.MockTransport(
        lambda req: httpx.Response(200, json={"data": [{"index": 0, "embedding": [1, 0]}]})
    )
    from fastapi import HTTPException

    with pytest.raises(HTTPException):
        await KnowledgeService(DB(), bad).embed(["第一段", "第二段"])


async def test_external_trace_parent_and_disabled_failure_are_nonfatal(monkeypatch):
    records = []

    class Client:
        def __init__(self, **kwargs):
            pass

        def create_run(self, **kwargs):
            records.append(kwargs)

        def update_run(self, run_id, **kwargs):
            pass

        def close(self):
            pass

    import langsmith

    monkeypatch.setattr(langsmith, "Client", Client)
    settings = Settings(_env_file=None, tracing_enabled=True, tracing_api_key="test-tracing-key")
    async with trace_span("root", settings) as root:
        async with trace_span("child", settings):
            assert correlation.get()["trace_id"]
    assert records[1]["parent_run_id"] == root
    assert correlation.get() == {}

    def broken(**kwargs):
        raise RuntimeError("service unavailable")

    monkeypatch.setattr(Client, "create_run", broken)
    async with trace_span("still_completes", settings):
        pass


@pytest.mark.integration
@pytest.mark.parametrize("wrong_nonce", [False, True])
async def test_oidc_pkce_signature_browser_binding_and_single_use(
    integration_settings, tmp_path, monkeypatch, wrong_nonce
):
    settings = integration_settings.model_copy(
        update={
            "oidc_issuer": "https://idp.example",
            "oidc_client_id": "media-client",
            "oidc_redirect_uri": "http://test/api/v1/auth/sso/callback",
            "log_dir": tmp_path,
        }
    )
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(private.public_key()))
    public.update(kid="test-key", alg="RS256", use="sig")
    state = {}
    original_client = httpx.AsyncClient

    def provider(request):
        if request.url.path.endswith("openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": settings.oidc_issuer,
                    "authorization_endpoint": "https://idp.example/authorize",
                    "token_endpoint": "https://idp.example/token",
                    "jwks_uri": "https://idp.example/keys",
                },
            )
        if request.url.path == "/keys":
            return httpx.Response(200, json={"keys": [public]})
        assert request.url.path == "/token"
        import base64
        import hashlib

        payload = parse_qs(request.content.decode())
        verifier = payload["code_verifier"][0]
        assert (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
            == state["challenge"]
        )
        now = datetime.now(timezone.utc)
        encoded = jwt.encode(
            {
                "iss": settings.oidc_issuer,
                "sub": "subject-" + uuid4().hex,
                "aud": settings.oidc_client_id,
                "iat": now,
                "exp": now + timedelta(minutes=5),
                "nonce": "wrong" if wrong_nonce else state["nonce"],
            },
            private,
            algorithm="RS256",
            headers={"kid": "test-key"},
        )
        return httpx.Response(200, json={"id_token": encoded})

    app = create_app(settings)
    async with app.router.lifespan_context(app):
        async with original_client(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as browser:
            monkeypatch.setattr(
                "app.api.sso.httpx.AsyncClient",
                lambda **kwargs: original_client(transport=httpx.MockTransport(provider), **kwargs),
            )
            response = await browser.get("/api/v1/auth/sso/login")
            assert response.status_code == 307
            params = parse_qs(urlsplit(response.headers["location"]).query)
            state.update(nonce=params["nonce"][0], challenge=params["code_challenge"][0])
            assert params["code_challenge_method"] == ["S256"]
            assert "code_verifier" not in params
            async with original_client(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as unbound:
                assert (
                    await unbound.get(
                        "/api/v1/auth/sso/callback",
                        params={"state": params["state"][0], "code": "authorization-code"},
                    )
                ).status_code == 401
            callback = await browser.get(
                "/api/v1/auth/sso/callback",
                params={"state": params["state"][0], "code": "authorization-code"},
            )
            if wrong_nonce:
                assert callback.status_code == 401
                assert (await browser.get("/api/v1/auth/me")).status_code == 401
            else:
                assert callback.status_code == 303 and callback.headers["location"] == "/app/"
                assert "HttpOnly" in callback.headers.get("set-cookie", "")
                me = await browser.get("/api/v1/auth/me")
                assert me.status_code == 200 and me.json()["role"] == "editor"
                assert (await browser.post("/api/v1/auth/logout")).status_code == 200
                assert (await browser.get("/api/v1/auth/me")).status_code == 401
            assert (
                await browser.get(
                    "/api/v1/auth/sso/callback",
                    params={"state": params["state"][0], "code": "authorization-code"},
                )
            ).status_code == 401


def test_actual_mcp_stdio_transport_without_paid_api_calls():
    result = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("stdio_probe.py"))],
        cwd=BACKEND_ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "stdio integration OK" in result.stdout


async def test_mcp_image_and_rag_tools_have_isolated_namespaces():
    from app.mcp_server import build_server

    with pytest.raises(ValueError, match="MEDIA_MCP_OWNER_ID"):
        build_server("rag", Settings(_env_file=None))
    image = build_server("image", Settings(_env_file=None))
    rag = build_server("rag", Settings(_env_file=None), owner_id="bound-namespace")
    assert [t.name for t in await image.list_tools()] == ["render_cards"]
    assert [t.name for t in await rag.list_tools()] == ["search_course"]


async def test_ark_embeddings_use_shared_key_and_independent_text_vectors():
    from app.core.embedding_client import Embedder

    observed = []

    async def provider(request):
        body = json.loads(request.content)
        observed.append(body)
        assert request.url.path == "/api/v3/embeddings/multimodal"
        assert request.headers["Authorization"] == "Bearer ark-embedding-test-key"
        assert len(body["input"]) == 1 and body["input"][0]["type"] == "text"
        assert "dimensions" not in body
        text = body["input"][0]["text"]
        if text == "第一段":
            await asyncio.sleep(0.02)
        return httpx.Response(
            200,
            json={
                "data": {"embedding": [1, 0] if text == "第一段" else [0, 1]},
                "usage": {"prompt_tokens": 3},
            },
        )

    settings = Settings(
        _env_file=None,
        embedding_provider="volcengine",
        ark_api_key="ark-embedding-test-key",
        ark_embedding_model="configured-embedding-model",
    )
    vectors = await Embedder(settings, httpx.MockTransport(provider)).embed(["第一段", "第二段"])
    assert vectors == [[1, 0], [0, 1]] and len(observed) == 2


def test_memory_checkpoints_cannot_replace_normal_demo_persistence():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(_env_file=None, checkpointer_backend="memory")
    value = Settings(
        _env_file=None,
        checkpointer_backend="memory",
        database_url="postgresql://user:pass@localhost/media_ops_test",
    )
    assert value.checkpointer_backend == "memory"


@pytest.mark.integration
def test_frozen_migration_ddl_creates_tables_in_rollback_schema(integration_settings):
    import ast

    import psycopg
    from psycopg import sql

    source = (BACKEND_ROOT / "alembic/versions/a6c48e202910_highlights.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    upgrade = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "upgrade")
    statements = [
        n.value.args[0].value
        for n in upgrade.body
        if isinstance(n, ast.Expr)
        and isinstance(n.value, ast.Call)
        and isinstance(n.value.func, ast.Attribute)
        and n.value.func.attr == "execute"
    ]
    schema = "migration_verify_" + uuid4().hex[:12]
    with psycopg.connect(integration_settings.database_url) as conn:
        conn.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        conn.execute(sql.SQL("SET LOCAL search_path TO {},public").format(sql.Identifier(schema)))
        for table in ["media_users", "media_tasks", "media_knowledge"]:
            conn.execute(
                sql.SQL("CREATE TABLE {} (LIKE public.{} INCLUDING ALL)").format(
                    sql.Identifier(table), sql.Identifier(table)
                )
            )
        for statement in statements:
            conn.execute(statement)
        assert conn.execute("SELECT to_regclass(%s)", (schema + ".media_evaluation_runs",)).fetchone()[0]
        assert conn.execute(
            "SELECT to_regclass(%s)", (schema + ".media_external_identities",)
        ).fetchone()[0]
        conn.rollback()
        assert conn.execute("SELECT to_regnamespace(%s)", (schema,)).fetchone()[0] is None
