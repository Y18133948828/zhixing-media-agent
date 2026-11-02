from unittest.mock import patch

import pytest
from dotenv import dotenv_values
from psycopg.conninfo import conninfo_to_dict

from app.configure import configure, database_url, provision, write_environment


def test_database_url_roundtrips_special_characters_and_ipv6():
    result = conninfo_to_dict(database_url("::1", 5432, "project", "some@user", "x@#:/%'中文"))
    assert result["host"] == "::1"
    assert result["user"] == "some@user"
    assert result["password"] == "x@#:/%'中文"


def test_existing_config_is_never_overwritten_or_probed(tmp_path):
    path = tmp_path / ".env"
    path.write_text("keep-me", encoding="utf-8")
    with patch("app.configure.psycopg.connect") as connect:
        configure(path, non_interactive=True)
        connect.assert_not_called()
    assert path.read_text() == "keep-me"


@pytest.mark.parametrize("role_exists", [True, False])
def test_provision_refuses_existing_names_without_mutating(role_exists):
    with patch("app.configure.psycopg.connect") as connect:
        conn = connect.return_value.__enter__.return_value
        conn.execute.return_value.fetchone.side_effect = [(1,)] if role_exists else [None, (1,)]
        with pytest.raises(ValueError, match="已存在"):
            provision("dbname=postgres", "demo", "demo_app", "not-a-real-password")
        assert all(str(call.args[0]).startswith("SELECT") for call in conn.execute.call_args_list)


def test_non_interactive_config_is_real_provider_and_does_not_reveal_key(tmp_path, monkeypatch, capsys):
    inputs = {
        "DATABASE_URL": database_url("localhost", 5432, "demo", "demo_app", "not@real#pass"),
        "ARK_API_KEY": "unit-test-key-not-a-credential",
        "ARK_TEXT_MODEL": "unit-text-model",
        "ARK_IMAGE_MODEL": "unit-image-model",
    }
    for name, value in inputs.items():
        monkeypatch.setenv(name, value)
    path = tmp_path / ".env"
    with patch("app.configure.psycopg.connect"):
        configure(path, non_interactive=True)
    values = dotenv_values(path, interpolate=False)
    assert values["LLM_PROVIDER"] == values["IMAGE_PROVIDER"] == "volcengine"
    assert values["API_KEY"] and len(values["API_KEY"]) >= 32
    assert all(values[name] == value for name, value in inputs.items())
    output = capsys.readouterr().out
    assert inputs["ARK_API_KEY"] not in output and inputs["DATABASE_URL"] not in output


def test_writer_preserves_values_and_refuses_overwrite(tmp_path):
    path = tmp_path / ".env"
    value = 'synthetic # value " quoted \\ end'
    write_environment(path, {"ARK_API_KEY": value})
    assert dotenv_values(path, interpolate=False)["ARK_API_KEY"] == value
    with pytest.raises(FileExistsError):
        write_environment(path, {"ARK_API_KEY": "different"})
