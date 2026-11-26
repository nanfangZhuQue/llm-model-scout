import os

import pytest

from llm_model_scout.agent.config import load_registry, prepare_token, remove_owned_token
from llm_model_scout.config import ConfigError


def test_registry_ignores_unrelated_route_environment(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(
        """[[providers]]
name="p"
[[providers.endpoints]]
id="e"
protocol="openai_chat"
base_url="https://approved.test/v1"
api_key_env="UPSTREAM_KEY"
""",
        encoding="utf-8",
    )
    registry = load_registry(
        path,
        {
            "UPSTREAM_KEY": "synthetic-key",
            "SCOUT_BASE_URL": "https://evil.test",
            "SCOUT_API_KEY": "different-key",
        },
    )
    assert registry["p"].endpoints[0].base_url == "https://approved.test/v1"
    assert registry["p"].endpoints[0].api_key == "synthetic-key"
    assert "synthetic-key" not in repr(registry)


def test_registry_rejects_reserved_credential_reference(tmp_path):
    path = tmp_path / "p.toml"
    path.write_text(
        """[[providers]]
name="p"
[[providers.endpoints]]
id="e"
protocol="openai_chat"
base_url="https://approved.test/v1"
api_key_env="SCOUT_BASE_URL"
""",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="reserved"):
        load_registry(path, {"SCOUT_BASE_URL": "https://unapproved.test/v1"})


def test_token_file_exclusive_and_cleanup(tmp_path):
    path = tmp_path / "agent.token"
    token, owned = prepare_token(None, path)
    assert len(token) >= 32 and path.read_text().strip() == token
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ConfigError):
        prepare_token(None, path)
    remove_owned_token(owned, token)
    assert not path.exists()


def test_token_cleanup_does_not_remove_changed_file(tmp_path):
    path = tmp_path / "agent.token"
    token, owned = prepare_token(None, path)
    path.write_text("changed-by-user", encoding="ascii")
    remove_owned_token(owned, token)
    assert path.read_text() == "changed-by-user"


def test_environment_token_no_file(tmp_path):
    token, path = prepare_token("AGENT_TOKEN", None, environ={"AGENT_TOKEN": "x" * 32})
    assert token == "x" * 32 and path is None
    with pytest.raises(ConfigError):
        prepare_token("AGENT_TOKEN", None, environ={"AGENT_TOKEN": "too-short"})
    with pytest.raises(ConfigError):
        prepare_token("AGENT_TOKEN", tmp_path / "token", environ={"AGENT_TOKEN": "x" * 32})
