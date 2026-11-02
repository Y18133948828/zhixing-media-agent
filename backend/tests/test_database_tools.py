from unittest.mock import patch

import pytest
from psycopg.conninfo import conninfo_to_dict

from app.core.config import Settings
from app.database_tools import ensure_database, info, replace_database
from app.database_tools import test_url as database_test_url


def settings(**values):
    return Settings(
        _env_file=None,
        database_url="postgresql://app:p%40ss%23word@127.0.0.1:5432/project?application_name=demo",
        **values,
    )


def test_connection_metadata_excludes_password_and_preserves_encoded_credentials():
    config = settings(database_mode="shared")
    public = info(config)
    assert public == {
        "mode": "shared",
        "host": "127.0.0.1",
        "port": 5432,
        "database": "project",
        "username": "app",
    }
    assert "p@ss#word" not in str(public)
    result = conninfo_to_dict(database_test_url(config))
    assert result["dbname"] == "project_test" and result["password"] == "p@ss#word"
    assert result["application_name"] == "demo"
    assert conninfo_to_dict(replace_database(config.database_url, "postgres"))["dbname"] == "postgres"


def test_shared_bootstrap_refuses_to_create_unapproved_database():
    with patch("app.database_tools.psycopg.connect") as connect:
        connection = connect.return_value.__enter__.return_value
        connection.execute.return_value.fetchone.return_value = None
        with pytest.raises(ValueError, match="共享实例尚无数据库"):
            ensure_database(settings(database_mode="shared"), testing=True)
        assert len(connection.execute.call_args_list) == 1


def test_oversized_test_database_name_is_rejected():
    config = Settings(_env_file=None, database_url="postgresql://app:fake@localhost/" + "x" * 62)
    with pytest.raises(ValueError, match="数据库名过长"):
        database_test_url(config)
