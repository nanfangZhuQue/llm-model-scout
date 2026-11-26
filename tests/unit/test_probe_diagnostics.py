import json
from dataclasses import replace

import httpx
import pytest

from llm_model_scout.adapters import InvalidResponse
from llm_model_scout.adapters import anthropic_messages as ant
from llm_model_scout.adapters import openai_chat as oai
from llm_model_scout.config import ConfigError, load_provider
from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProbeConfig, ProviderConfig
from llm_model_scout.report import report_json


def completion(content=None, finish=None, **message):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": content, **message},
                "finish_reason": finish,
            }
        ]
    }


def anthropic(content, stop=None):
    return {"type": "message", "role": "assistant", "content": content, "stop_reason": stop}


@pytest.mark.parametrize(
    "adapter,data,code,stop",
    [
        (oai, completion("", "length"), "token_limit_without_text", "length"),
        (oai, completion(" ", "stop"), "empty_text", "stop"),
        (
            oai,
            completion(None, "tool_calls", tool_calls=[{"id": "t"}]),
            "non_text_output",
            "tool_calls",
        ),
        (oai, completion(None, None, reasoning="private"), "non_text_output", None),
        (oai, completion("refusal text", "content_filter"), "refusal", "content_filter"),
        (oai, completion("text", "stop", refusal="private refusal"), "refusal", "stop"),
        (oai, {"choices": []}, "invalid_structure", None),
        (oai, completion(42, "length"), "invalid_structure", "length"),
        (ant, anthropic([], "max_tokens"), "token_limit_without_text", "max_tokens"),
        (ant, anthropic([{"type": "text", "text": ""}], "end_turn"), "empty_text", "end_turn"),
        (ant, anthropic([{"type": "thinking", "thinking": "private"}]), "non_text_output", None),
        (ant, anthropic([{"type": "text", "text": "declined"}], "refusal"), "refusal", "refusal"),
        (ant, {"content": []}, "invalid_structure", None),
    ],
)
def test_failure_diagnostics(adapter, data, code, stop):
    with pytest.raises(InvalidResponse) as caught:
        adapter.validate_text(data)
    assert caught.value.code == code
    assert caught.value.stop_reason == stop
    assert caught.value.suggestion
    assert "private" not in str(caught.value)


@pytest.mark.parametrize(
    "adapter,data,expected",
    [
        (oai, completion("pong", "length"), "length"),
        (ant, anthropic([{"type": "text", "text": "pong"}], "max_tokens"), "max_tokens"),
        (oai, completion("pong", "untrusted-secret"), None),
        (ant, anthropic([{"type": "text", "text": "pong"}], {"injected": "secret"}), None),
    ],
)
def test_success_stop_reason_is_whitelisted(adapter, data, expected):
    assert adapter.validate_text(data) == expected


def test_valid_choice_wins_over_separate_failed_choice():
    data = completion(None, "length")
    data["choices"] += completion("pong", "stop")["choices"]
    assert oai.validate_text(data) == "stop"


def config_options(**extra):
    return {
        "protocol": "openai_chat",
        "base_url": "https://example.test",
        "api_key_env": "KEY",
        **extra,
    }


@pytest.mark.parametrize(
    "value", [0, -1, True, 1.5, 1.0, "1.5", "1.0", "nan", "inf", "", "-1", "secret-value"]
)
def test_invalid_token_budget(value):
    with pytest.raises(ConfigError, match="max_tokens") as caught:
        load_provider(config_options(max_tokens=value), {"KEY": "test-key"})
    assert "secret-value" not in str(caught.value)


def test_token_budget_sources_and_profiles(tmp_path):
    file = tmp_path / "provider.toml"
    file.write_text(
        """[[providers]]
name = "p"
[[providers.endpoints]]
id = "a"
protocol = "openai_chat"
base_url = "https://example.test/v1"
api_key_env = "KEY"
profile = "chat-basic"
max_tokens = 32
""",
        encoding="utf-8",
    )
    assert load_provider({"config": file}, {"KEY": "test"}).endpoints[0].probe.max_tokens == 32
    env = {"KEY": "test", "SCOUT_MAX_TOKENS": "64"}
    assert load_provider({"config": file}, env).endpoints[0].probe.max_tokens == 64
    probe = load_provider({"config": file, "max_tokens": "128"}, env).endpoints[0].probe
    assert probe.max_tokens == 128 and probe.temperature is None
    assert probe.profile_id == "chat-basic"
    assert load_provider(config_options(), {"KEY": "test"}).endpoints[0].probe.max_tokens == 1


@pytest.mark.parametrize(
    "protocol,body",
    [
        ("openai_chat", completion(None, "length")),
        ("anthropic_messages", anthropic([], "max_tokens")),
    ],
)
async def test_budget_payload_and_diagnostics_report(protocol, body):
    endpoint = EndpointConfig(
        "e",
        protocol,
        "https://example.test/v1",
        "synthetic-secret",
        models=("m",),
        probe=replace(ProbeConfig.named("chat-basic"), max_tokens=128),
    )
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    report = await scan(
        ProviderConfig("p", (endpoint,)), http_transport=httpx.MockTransport(handler)
    )
    assert len(sent) == 1  # no automatic budget retry
    assert sent[0]["max_tokens"] == 128 and "temperature" not in sent[0]
    assert report.results[0].diagnostic_code == "token_limit_without_text"
    assert report.results[0].error_category == "BAD_RESPONSE"
    assert report.results[0].reason == "probe_failed"
    assert report.endpoints[0]["probe"]["max_tokens"] == 128
    assert "synthetic-secret" not in report_json(report)


async def test_unknown_reason_and_private_content_never_exported():
    endpoint = EndpointConfig(
        "e", "openai_chat", "https://example.test/v1", "synthetic-secret", models=("m",)
    )
    payload = completion(None, "synthetic-secret", reasoning="PRIVATE_MODEL_REASONING")
    report = await scan(
        ProviderConfig("p", (endpoint,)),
        http_transport=httpx.MockTransport(lambda r: httpx.Response(200, json=payload)),
    )
    text = report_json(report)
    assert "synthetic-secret" not in text and "PRIVATE_MODEL_REASONING" not in text
    assert report.results[0].stop_reason is None
    assert report.results[0].diagnostic_code == "non_text_output"
