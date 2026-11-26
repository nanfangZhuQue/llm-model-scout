import pytest

from llm_model_scout.config import ConfigError, load_provider, normalize_url
from llm_model_scout.core.redact import Redactor


@pytest.mark.parametrize(
    "value,expected",
    [
        ("https://example.test/", "https://example.test/v1"),
        ("https://example.test/v1/", "https://example.test/v1"),
        ("https://example.test/custom/v1", "https://example.test/custom/v1"),
    ],
)
def test_url(value, expected):
    assert normalize_url(value) == expected


@pytest.mark.parametrize(
    "value",
    [
        "file:///a",
        "https://u:p@example.test",
        "https://a.test?key=x",
        "https://a.test/#fragment",
        "https://a.test:bad",
        "https://a.test/\n",
    ],
)
def test_invalid_url(value):
    with pytest.raises(ConfigError):
        normalize_url(value)


def options(**kwargs):
    return {
        "protocol": "openai_chat",
        "base_url": "https://example.test",
        "api_key_env": "TEST_KEY",
        **kwargs,
    }


def test_priority_and_empty_credentials():
    provider = load_provider(
        options(timeout="2"), {"TEST_KEY": "synthetic-secret", "SCOUT_TIMEOUT": "3"}
    )
    assert provider.policy.timeout == 2
    assert provider.policy.concurrency == 10
    assert "synthetic-secret" not in repr(provider)
    with pytest.raises(ConfigError):
        load_provider(options(), {})


@pytest.mark.parametrize(
    "name,value",
    [
        ("concurrency", "0"),
        ("retries", "-1"),
        ("timeout", "nan"),
        ("concurrency", "1.5"),
        ("timeout", "inf"),
    ],
)
def test_invalid_numbers(name, value):
    with pytest.raises(ConfigError):
        load_provider(options(**{name: value}), {"TEST_KEY": "secret"})


def test_manual_models_preserve_case_and_dedupe(tmp_path):
    path = tmp_path / "models.txt"
    path.write_text("X\nx\nX\n\n", encoding="utf-8")
    parsed = load_provider(options(models_file=path), {"TEST_KEY": "secret"})
    assert parsed.endpoints[0].models == ("X", "x")


def test_responses_has_its_own_default_and_honors_probe_overrides():
    env = {"TEST_KEY": "synthetic-key", "SCOUT_PROTOCOL": "openai_responses"}
    provider = load_provider(options(protocol=None), env)
    (endpoint,) = provider.endpoints
    assert endpoint.protocol == "openai_responses"
    assert (endpoint.probe.profile_id, endpoint.probe.max_tokens, endpoint.probe.temperature) == (
        "chat-basic",
        16,
        None,
    )
    (override,) = load_provider(
        options(protocol=None, profile="baseline", max_tokens=24), env
    ).endpoints
    assert (override.probe.profile_id, override.probe.max_tokens, override.probe.temperature) == (
        "baseline",
        24,
        0,
    )


@pytest.mark.parametrize("capability", ["structured", "tool"])
def test_schema_probe_cli_defaults_have_room_for_json_and_keep_explicit_caps(capability):
    env = {"TEST_KEY": "synthetic-key", "SCOUT_CAPABILITY": capability}
    (endpoint,) = load_provider(options(), env).endpoints
    assert endpoint.probe.capability == capability
    assert endpoint.probe.profile_id == "chat-basic" and endpoint.probe.max_tokens == 128
    (profile,) = load_provider(options(profile="baseline"), env).endpoints
    assert profile.probe.max_tokens == 1 and profile.probe.temperature == 0
    (override,) = load_provider(options(max_tokens=64), env).endpoints
    assert override.probe.max_tokens == 64 and override.probe.temperature is None
    (text,) = load_provider(options(capability="text"), env).endpoints
    assert text.probe.capability == "text" and text.probe.max_tokens == 1


def test_endpoint_selection_and_secrets(tmp_path):
    config = tmp_path / "p.toml"
    config.write_text(
        """[[providers]]
name = "p"
[[providers.endpoints]]
id = "a"
protocol = "openai_chat"
base_url = "https://a.test/v1"
api_key_env = "KEY_A"
[[providers.endpoints]]
id = "b"
protocol = "anthropic_messages"
base_url = "https://b.test/v1"
api_key_env = "KEY_B"
""",
        encoding="utf-8",
    )
    env = {"KEY_A": "secret-a", "KEY_B": "secret-b"}
    with pytest.raises(ConfigError):
        load_provider({"config": config}, env)
    provider = load_provider({"config": config, "endpoint": "all"}, env)
    assert len(provider.endpoints) == 2
    with pytest.raises(ConfigError):
        load_provider({"config": config, "endpoint": "all", "base_url": "https://other.test"}, env)


def test_redaction():
    redactor = Redactor(["synthetic-key", "pass word"])
    text = redactor.text("synthetic-key pass%20word Bearer another-secret\x1b[31m\n")
    assert "synthetic-key" not in text
    assert "pass%20word" not in text
    assert "another-secret" not in text
    assert "\x1b" not in text
