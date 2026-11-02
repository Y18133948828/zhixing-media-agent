"""First-run local configuration. Never replaces an existing .env or resets DB users."""

import argparse
import getpass
import os
import re
import secrets
from urllib.parse import quote

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from app.core.config import BACKEND_ROOT


def database_url(host, port, database, username, password):
    address = f"[{host}]" if ":" in host else host
    return (
        f"postgresql://{quote(username, safe='')}:{quote(password, safe='')}"
        f"@{address}:{port}/{quote(database, safe='')}"
    )


def validate_name(value):
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,57}", value):
        raise ValueError("库名和账号名请使用小写字母开头、仅字母/数字/下划线，最多58个字符。")
    return value


def provision(admin_conninfo, database, username, password, *, with_test=False):
    """Explicit first-run action only. Existing names cause refusal, never overwrite."""
    validate_name(database)
    validate_name(username)
    names = [database, database + "_test"] if with_test else [database]
    with psycopg.connect(admin_conninfo, autocommit=True, connect_timeout=5) as conn:
        if conn.execute("SELECT 1 FROM pg_roles WHERE rolname=%s", (username,)).fetchone():
            raise ValueError("该普通账号已存在；请选择已有账号模式或换一个项目账号，不会修改其密码。")
        for name in names:
            if conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
                raise ValueError("目标数据库已存在；请选择已有账号模式或更换库名，不会清空数据。")
        conn.execute(
            sql.SQL("CREATE ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {}").format(
                sql.Identifier(username), sql.Literal(password)
            )
        )
        for name in names:
            conn.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(name), sql.Identifier(username)
                )
            )


def prompt(label, default=""):
    answer = input(f"{label}" + (f" [{default}]" if default else "") + "：").strip()
    return answer or default


def required_secret(label):
    value = getpass.getpass(label + "：").strip()
    if not value:
        raise ValueError(label + "不能为空。")
    return value


def write_environment(path, values):
    # Quotes handle #, spaces, quotes and non-ASCII without shell interpolation.
    import json

    body = (BACKEND_ROOT / ".env.example").read_text(encoding="utf-8")
    for name, value in values.items():
        encoded = json.dumps(str(value), ensure_ascii=False)
        body, count = re.subn(
            rf"(?m)^{re.escape(name)}=.*$", lambda match: name + "=" + encoded, body
        )
        if count != 1:
            raise ValueError("环境变量模板字段缺失或重复：" + name)
    # O_EXCL prevents accidental overwrite, even if another setup wins a race.
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(body)


def configure(path, *, non_interactive=False):
    if path.exists():
        print("保留已有 backend/.env，不覆盖数据库与模型配置。")
        return
    if non_interactive:
        names = ["DATABASE_URL", "ARK_API_KEY", "ARK_TEXT_MODEL", "ARK_IMAGE_MODEL"]
        if any(not os.environ.get(name, "").strip() for name in names):
            raise ValueError("非交互安装需设置 DATABASE_URL、ARK_API_KEY、ARK_TEXT_MODEL、ARK_IMAGE_MODEL。")
        values = {name: os.environ[name] for name in names}
    else:
        print("配置火山方舟真实服务；密钥输入不显示，也不会发给前端。")
        # Collect cloud fields before provisioning, so a cancelled prompt leaves no new DB role.
        values = {
            "ARK_API_KEY": required_secret("方舟 API Key（不是账号 AK/SK）"),
            "ARK_TEXT_MODEL": prompt("文字模型 Model ID / Endpoint ID"),
            "ARK_IMAGE_MODEL": prompt("生图模型 Model ID / Endpoint ID"),
        }
        if not values["ARK_TEXT_MODEL"] or not values["ARK_IMAGE_MODEL"]:
            raise ValueError("文字和生图模型 ID 均需填写，从自己的方舟控制台复制。")
        host = prompt("PostgreSQL 主机", "127.0.0.1")
        port = int(prompt("端口", "5432"))
        database = validate_name(prompt("本项目独立数据库名", "zhixing_media"))
        mode = prompt("1=使用已有普通账号，2=用管理员创建专用库和账号", "1")
        if mode == "1":
            username = prompt("项目普通账号", "zhixing_media")
            password = required_secret("项目数据库密码")
        elif mode == "2":
            username = validate_name(prompt("新建项目普通账号", database + "_app"))
            admin = prompt("PostgreSQL 管理员账号", "postgres")
            admin_password = required_secret("管理员密码（仅用于这次建库，不保存）")
            password = secrets.token_urlsafe(32)
            with_test = prompt("同时建立独立测试库？y/n", "n").lower() == "y"
            provision(
                make_conninfo(host=host, port=port, dbname="postgres", user=admin, password=admin_password),
                database, username, password, with_test=with_test,
            )
        else:
            raise ValueError("请选择1或2。")
        values["DATABASE_URL"] = database_url(host, port, database, username, password)
    conninfo_to_dict(values["DATABASE_URL"])
    if not values["DATABASE_URL"].startswith("postgresql://"):
        raise ValueError("DATABASE_URL 需要 postgresql:// 连接串。")
    with psycopg.connect(values["DATABASE_URL"], connect_timeout=5) as conn:
        conn.execute("SELECT 1")
    values.update(API_KEY=secrets.token_urlsafe(48), LLM_PROVIDER="volcengine", IMAGE_PROVIDER="volcengine")
    write_environment(path, values)
    print("已创建 backend/.env。模型权限将在首次真实调用时验证；配置阶段不调用付费模型。")


def main():
    parser = argparse.ArgumentParser(description="本地数据库与火山方舟配置向导")
    parser.add_argument("--non-interactive", action="store_true")
    args = parser.parse_args()
    try:
        configure(BACKEND_ROOT / ".env", non_interactive=args.non_interactive)
    except (ValueError, OSError, psycopg.Error) as exc:
        # A psycopg exception can include server/connection details; never print raw credentials.
        message = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
        print("配置未完成：" + message + "。请检查 PostgreSQL、权限和输入值后重试。")
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        print("\n配置已取消；已有配置不会被覆盖。")
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
