"""Database bootstrap helpers; connection credentials are never included in diagnostics."""

import argparse
import json
from urllib.parse import quote, urlsplit, urlunsplit

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

from app.core.config import Settings


def replace_database(url, name):
    parts = urlsplit(url)
    return urlunsplit(parts._replace(path="/" + quote(name, safe="")))


def info(settings):
    connection = conninfo_to_dict(settings.database_url)
    return {
        "mode": settings.database_mode,
        "host": connection.get("host", "localhost"),
        "port": int(connection.get("port", 5432)),
        "database": connection.get("dbname", ""),
        "username": connection.get("user", ""),
    }


def test_url(settings):
    name = info(settings)["database"] + "_test"
    if len(name.encode()) > 63:
        raise ValueError("数据库名过长，不能安全派生独立测试库")
    return replace_database(settings.database_url, name)


def ensure_database(settings, *, testing=False):
    target = test_url(settings) if testing else settings.database_url
    name = conninfo_to_dict(target)["dbname"]
    with psycopg.connect(
        replace_database(target, "postgres"), autocommit=True, connect_timeout=5
    ) as conn:
        exists = conn.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone()
        if not exists:
            if settings.database_mode == "shared":
                raise ValueError(f"共享实例尚无数据库 {name}；请用公共数据库管理工具先创建并授权")
            conn.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    with psycopg.connect(target, connect_timeout=5) as conn:
        conn.execute("SELECT 1")
    return name


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["info", "check", "ensure", "prepare-test", "test-url"])
    args = parser.parse_args()
    settings = Settings(llm_provider="mock", image_provider="template")
    if args.action == "info":
        print(json.dumps(info(settings), ensure_ascii=False))
    elif args.action == "test-url":
        # Consumed only by the test launcher into an environment variable, never logged.
        print(test_url(settings))
    elif args.action in ("ensure", "prepare-test"):
        print("数据库已就绪：" + ensure_database(settings, testing=args.action == "prepare-test"))
    else:
        with psycopg.connect(settings.database_url, connect_timeout=5) as conn:
            row = conn.execute(
                "SELECT current_database(), current_user, current_setting('server_version')"
            ).fetchone()
        print(
            json.dumps(
                {**info(settings), "database": row[0], "username": row[1], "version": row[2]},
                ensure_ascii=False,
            )
        )


if __name__ == "__main__":
    try:
        main()
    except ValueError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except Exception as exc:
        print(f"数据库操作失败（{type(exc).__name__}）。请检查实例运行状态和本地连接配置。")
        raise SystemExit(1) from None
