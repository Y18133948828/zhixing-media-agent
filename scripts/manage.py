"""Cross-platform local launcher. Run with Python 3.11+; no global packages needed."""

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
PYTHON = BACKEND / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def run(*args, cwd=ROOT, env=None):
    # Do not echo arguments: a caller's connection configuration may be sensitive.
    subprocess.run([str(value) for value in args], cwd=cwd, env=env, check=True)


def node_tools():
    node = shutil.which("node")
    npm = shutil.which("npm.cmd" if os.name == "nt" else "npm")
    if not node or not npm:
        raise ValueError("请先安装 Node.js 22.12+（含 npm），并重新打开终端。")
    version = subprocess.check_output([node, "-p", "process.versions.node"], text=True).strip()
    major, minor = (int(part) for part in version.split(".")[:2])
    if major < 22 or (major == 22 and minor < 12):
        raise ValueError("需要 Node.js 22.12+；建议使用 Node.js 22 LTS。")
    return npm


def require_environment():
    if not PYTHON.exists() or not (BACKEND / ".env").exists():
        raise ValueError("尚未完成安装，请先运行 python scripts/manage.py setup。")


def build(npm):
    run(npm, "ci", "--no-audit", "--no-fund", cwd=ROOT / "frontend")
    run(npm, "run", "build", cwd=ROOT / "frontend")


def main():
    parser = argparse.ArgumentParser(description="知行 · AI 内容工作室：本地安装、启动与测试")
    parser.add_argument("action", choices=["setup", "start", "build", "test", "check"])
    parser.add_argument("--non-interactive", action="store_true", help="setup 从环境变量读取配置，不询问密码")
    args = parser.parse_args()
    if not (3, 11) <= sys.version_info[:2] < (3, 15):
        raise ValueError("需要 Python 3.11–3.14；当前验收使用 Python 3.13。")
    os.environ["PYTHONUTF8"] = "1"
    if args.action == "setup":
        npm = node_tools()
        if not PYTHON.exists():
            run(sys.executable, "-m", "venv", BACKEND / ".venv")
        run(PYTHON, "-m", "pip", "install", "-r", BACKEND / "requirements.lock")
        run(PYTHON, "-m", "pip", "install", "--no-deps", "-e", BACKEND)
        configure_args = ["--non-interactive"] if args.non_interactive else []
        run(PYTHON, "-m", "app.configure", *configure_args, cwd=BACKEND)
        run(PYTHON, "-m", "app.database_tools", "check", cwd=BACKEND)
        run(PYTHON, "-m", "app.init_db", cwd=BACKEND)
        run(PYTHON, "-m", "playwright", "install", "chromium", cwd=BACKEND)
        build(npm)
        print("安装完成。运行 python scripts/manage.py start，然后打开 http://127.0.0.1:8000/app/。")
    elif args.action == "build":
        build(node_tools())
    else:
        require_environment()
        if args.action == "start":
            if not (ROOT / "frontend/dist/index.html").exists():
                build(node_tools())
            run(PYTHON, "-m", "app.database_tools", "check", cwd=BACKEND)
            run(PYTHON, "-m", "app.init_db", cwd=BACKEND)
            run(PYTHON, "-m", "app.run", cwd=BACKEND)
        else:
            if args.action == "check":
                run(PYTHON, "-m", "ruff", "check", "app", "tests", "alembic", cwd=BACKEND)
                run(PYTHON, "-m", "pip", "check", cwd=BACKEND)
            # Explicitly scoped disposable database; never infer an arbitrary production URL.
            if "TEST_DATABASE_URL" not in os.environ:
                raise ValueError("测试需要 TEST_DATABASE_URL，数据库名称必须以 _test 结尾；见安装指南。")
            run(PYTHON, "-m", "pytest", "-q", cwd=BACKEND)
            if args.action == "check":
                build(node_tools())


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError) as exc:
        print(f"操作未完成：{exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except subprocess.CalledProcessError:
        print("操作未完成，请查看上面的错误并按安装指南修复后重试。", file=sys.stderr)
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n已停止。PostgreSQL 服务不会被关闭。")
