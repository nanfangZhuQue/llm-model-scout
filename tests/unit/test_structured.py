import pytest

from llm_model_scout.adapters import InvalidResponse, adapter_for
from llm_model_scout.adapters.structured import request_body, validate_output
from llm_model_scout.domain import PROTOCOLS, STRUCTURED_PROMPT, EndpointConfig, ProbeConfig

VALID = '{"status":"ok","count":1}'
SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}, "count": {"type": "integer"}},
    "required": ["status", "count"],
    "additionalProperties": False,
}


def _chat(text, stop="stop", **message):
    return {
        "choices": [
            {
                "message": {"role": "assistant", "content": text, **message},
                "finish_reason": stop,
            }
        ]
    }


def _anthropic(text, stop="end_turn"):
    blocks = text if isinstance(text, list) else [{"type": "text", "text": text}]
    return {"type": "message", "role": "assistant", "content": blocks, "stop_reason": stop}


def _responses(text, status="completed", **fields):
    blocks = text if isinstance(text, list) else [{"type": "output_text", "text": text}]
    return {
        "object": "response",
        "status": status,
        "output": [{"type": "message", "role": "assistant", "content": blocks}],
        **fields,
    }


CONTRACTS = [
    ("openai_chat", _chat, "stop"),
    ("openai_responses", _responses, "completed"),
    ("anthropic_messages", _anthropic, "end_turn"),
]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_structured_request_uses_fixed_schema_and_keeps_original_settings(protocol):
    endpoint = EndpointConfig(
        "endpoint",
        protocol,
        "https://provider.example/v1",
        "synthetic-unit-key",
        probe=ProbeConfig("chat-basic", 128, None, "other prompt"),
    )
    original = adapter_for(endpoint).request_body("test-model", endpoint.probe)
    request = request_body(endpoint, "test-model")
    if protocol == "openai_chat":
        assert request.pop("response_format") == {
            "type": "json_schema",
            "json_schema": {"name": "scout_structured_v1", "strict": True, "schema": SCHEMA},
        }
    elif protocol == "openai_responses":
        assert request.pop("text") == {
            "format": {
                "type": "json_schema",
                "name": "scout_structured_v1",
                "strict": True,
                "schema": SCHEMA,
            }
        }
        assert request["store"] is False and request["max_output_tokens"] == 128
    else:
        assert request.pop("output_config") == {"format": {"type": "json_schema", "schema": SCHEMA}}
    if protocol == "openai_responses":
        assert request.pop("input") == STRUCTURED_PROMPT
        assert original.pop("input") == "other prompt"
    else:
        assert request.pop("messages") == [{"role": "user", "content": STRUCTURED_PROMPT}]
        assert original.pop("messages") == [{"role": "user", "content": "other prompt"}]
    assert request["stream"] is False
    assert request == original
    assert endpoint.probe.prompt == "other prompt"


@pytest.mark.parametrize("protocol, wrap, stop", CONTRACTS)
@pytest.mark.parametrize("count", ["1", "1.0", "1e3", "-2"])
def test_structured_passes_integer_instances(protocol, wrap, stop, count):
    assert validate_output(protocol, wrap(f'{{"status":"ok","count":{count}}}')) == stop


@pytest.mark.parametrize("protocol, wrap, stop", CONTRACTS)
@pytest.mark.parametrize(
    "value",
    [
        '{"status":"ok","count":"upstream-secret-value"}',
        '{"status":"ok","count":true}',
        '{"status":"ok","count":1.000000000000000000001}',
        '{"status":"bad","count":1}',
        '{"status":"ok","count":1,"extra":false}',
        '{"status":"ok"}',
        "[]",
        "null",
    ],
)
def test_parseable_but_wrong_schema_is_reported_separately(protocol, wrap, stop, value):
    with pytest.raises(InvalidResponse) as issue:
        validate_output(protocol, wrap(value))
    assert issue.value.code == "schema_mismatch"
    assert issue.value.stop_reason == stop
    assert "upstream-secret-value" not in str(issue.value)


@pytest.mark.parametrize("protocol, wrap, stop", CONTRACTS)
@pytest.mark.parametrize(
    "value",
    [
        f"```json\n{VALID}\n```",
        f"{VALID} {VALID}",
        '{"status":"ok","count":NaN}',
        '{"status":"ok","count":1,"count":2}',
        '{"status":"upstream-secret-value"',
    ],
)
def test_invalid_json_is_reported_without_publishing_contents(protocol, wrap, stop, value):
    with pytest.raises(InvalidResponse) as issue:
        validate_output(protocol, wrap(value))
    assert issue.value.code == "invalid_structured_json"
    assert issue.value.stop_reason == stop
    assert "upstream-secret-value" not in str(issue.value)


@pytest.mark.parametrize(
    "protocol, reply, code, stop",
    [
        ("openai_chat", _chat(VALID, "length"), "incomplete_response", "length"),
        ("openai_chat", _chat(VALID, "tool_calls"), "incomplete_response", "tool_calls"),
        ("openai_chat", _chat(None, refusal="upstream-secret-value"), "refusal", "stop"),
        (
            "anthropic_messages",
            _anthropic(VALID, "max_tokens"),
            "incomplete_response",
            "max_tokens",
        ),
        ("anthropic_messages", _anthropic(VALID, "refusal"), "refusal", "refusal"),
        ("openai_responses", _responses(VALID, "incomplete"), "incomplete_response", "incomplete"),
        (
            "openai_responses",
            _responses([{"type": "refusal", "refusal": "upstream-secret-value"}]),
            "refusal",
            "completed",
        ),
    ],
)
def test_termination_and_refusal_precede_schema_validation(protocol, reply, code, stop):
    with pytest.raises(InvalidResponse) as issue:
        validate_output(protocol, reply)
    assert (issue.value.code, issue.value.stop_reason) == (code, stop)
    assert "upstream-secret-value" not in str(issue.value)


def test_chat_refusal_block_overrides_valid_json_in_same_message():
    data = _chat([{"type": "text", "text": VALID}, {"type": "refusal", "refusal": "test refusal"}])
    with pytest.raises(InvalidResponse) as issue:
        validate_output("openai_chat", data)
    assert issue.value.code == "refusal"


@pytest.mark.parametrize(
    "protocol, wrap", [("openai_chat", _chat), ("anthropic_messages", _anthropic)]
)
@pytest.mark.parametrize("stop", [{"reason": "max_tokens"}, "unknown_stop"])
def test_malformed_or_unknown_stop_cannot_turn_valid_json_into_success(protocol, wrap, stop):
    with pytest.raises(InvalidResponse) as issue:
        validate_output(protocol, wrap(VALID, stop))
    assert issue.value.code == "invalid_structure"


def test_chat_checks_each_choice_and_accepts_the_valid_one():
    incomplete = _chat(VALID, "length")["choices"]
    wrong = _chat('{"status":"ok","count":"2"}')["choices"]
    valid = _chat([{"type": "text", "text": text} for text in (VALID[:10], VALID[10:])])["choices"]
    assert validate_output("openai_chat", {"choices": incomplete + wrong + valid}) == "stop"
