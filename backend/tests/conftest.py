import os
import subprocess
import sys

import pytest

from app.core.config import BACKEND_ROOT


@pytest.fixture(scope="session")
def integration_settings(tmp_path_factory):
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("设置 TEST_DATABASE_URL 或运行 scripts/test.ps1 执行真实 PostgreSQL 集成测试")
    if not url.rsplit("/", 1)[-1].endswith("_test"):
        pytest.fail("集成测试数据库名称必须以 _test 结尾")
    environment = {
        **os.environ, "DATABASE_URL": url, "LLM_PROVIDER": "mock",
        "IMAGE_PROVIDER": "template", "PYTHONUTF8": "1",
    }
    result = subprocess.run(
        [sys.executable, "-m", "app.init_db"],
        cwd=BACKEND_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    from app.core.config import Settings

    return Settings(
        _env_file=None,
        database_url=url,
        llm_provider="mock",
        api_key="integration-test-api-key",
        worker_poll_seconds=0.1,
        login_limit_per_minute=1000,
        rate_limit_per_minute=100000,
        storage_dir=tmp_path_factory.mktemp("assets"),
    )
