from copy import deepcopy
from json import dumps

import pytest

from llm_model_scout.adapters import InvalidResponse
from llm_model_scout.adapters.json_values import load_probe_json
from llm_model_scout.adapters.tool_call import request_body, validate_output
from llm_model_scout.domain import PROTOCOLS, TOOL_PROMPT, EndpointConfig, ProbeConfig

TOOL = "scout_tool_probe"
ARGS = {"value": "ping", "count": 1}
SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["ping"]}, "count": {"type": "integer"}},
    "required": ["value", "count"],
    "additionalProperties": False,
}
STOPS = {
    "openai_chat": "tool_calls",
    "anthropic_messages": "tool_use",
    "openai_responses": "completed",
}


def _build_response(protocol, name=TOOL, arguments=None, *, calls=1):
    arguments = ARGS if arguments is None else arguments
    if protocol != "anthropic_messages":
        encoded = arguments if isinstance(arguments, str) else dumps(arguments)
    if protocol == "openai_chat":
        return {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None if calls else "text only",
                        "tool_calls": [
                            {
                                "id": f"call_{index}",
                                "type": "function",
                                "function": {"name": name, "arguments": encoded},
                            }
                            for index in range(calls)
                        ],
                    },
                    "finish_reason": "tool_calls" if calls else "stop",
                }
            ]
        }
    if protocol == "anthropic_messages":
        content = [
            {"type": "tool_use", "id": f"toolu_{index}", "name": name, "input": arguments}
            for index in range(calls)
        ]
        return {
            "type": "message",
            "role": "assistant",
            "stop_reason": "tool_use" if calls else "end_turn",
            "content": content or [{"type": "text", "text": "text only"}],
        }
    output = [
        {
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": name,
            "arguments": encoded,
            "status": "completed",
        }
        for index in range(calls)
    ]
    return {
        "object": "response",
        "status": "completed",
        "output": output
        or [
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": "text only"}],
            }
        ],
    }


def _check_error(protocol, response, code):
    with pytest.raises(InvalidResponse) as exc:
        validate_output(protocol, response)
    assert exc.value.code == code
    assert "private-upstream-text" not in str(exc.value)
    return exc.value


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_tool_request_uses_one_synthetic_definition_and_forces_named_choice(protocol):
    endpoint = EndpointConfig(
        "endpoint",
        protocol,
        "https://provider.example/v1",
        "synthetic-unit-key",
        probe=ProbeConfig("chat-basic", 128, None, "unused prompt"),
    )
    body = request_body(endpoint, "test-model")
    (tool,) = body["tools"]
    assert body["model"] == "test-model" and body["stream"] is False
    assert "temperature" not in body
    assert endpoint.probe.prompt == "unused prompt"
    if protocol == "openai_chat":
        assert tool["type"] == "function"
        assert body["tool_choice"] == {"type": "function", "function": {"name": TOOL}}
        assert body["parallel_tool_calls"] is False
        assert body["messages"] == [{"role": "user", "content": TOOL_PROMPT}]
        assert body["max_tokens"] == 128
        tool = tool["function"]
    elif protocol == "openai_responses":
        assert tool["type"] == "function"
        assert body["tool_choice"] == {"type": "function", "name": TOOL}
        assert body["parallel_tool_calls"] is False
        assert body["input"] == TOOL_PROMPT
        assert body["max_output_tokens"] == 128 and body["store"] is False
    else:
        assert body["tool_choice"] == {
            "type": "tool",
            "name": TOOL,
            "disable_parallel_tool_use": True,
        }
        assert body["messages"] == [{"role": "user", "content": TOOL_PROMPT}]
        assert body["max_tokens"] == 128
    assert tool["name"] == TOOL and tool["description"] and tool["strict"] is True
    key = "input_schema" if protocol == "anthropic_messages" else "parameters"
    assert tool[key] == SCHEMA
    tool[key]["required"].append("unexpected")
    assert request_body(endpoint, "test-model") != body


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_only_one_complete_tool_with_the_expected_name_passes(protocol):
    assert validate_output(protocol, _build_response(protocol)) == STOPS[protocol]
    _check_error(protocol, _build_response(protocol, calls=0), "no_tool_call")
    _check_error(protocol, _build_response(protocol, calls=2), "unexpected_tool_call_count")
    _check_error(
        protocol, _build_response(protocol, "private-upstream-text"), "unexpected_tool_name"
    )


@pytest.mark.parametrize("protocol", PROTOCOLS)
@pytest.mark.parametrize(
    "arguments",
    [
        {"value": "ping", "count": "private-upstream-text"},
        {"value": "ping", "count": True},
        {"value": "pong", "count": 1},
        {"value": "ping", "count": 1, "extra": 1},
        {"value": "ping"},
        ["ping", 1],
    ],
)
def test_tool_argument_schema_fails_without_exposing_value(protocol, arguments):
    _check_error(protocol, _build_response(protocol, arguments=arguments), "invalid_tool_arguments")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_fractional_counts_do_not_pass_after_rounding(protocol):
    valid = '{"value":"ping","count":1.0}'
    invalid = '{"value":"ping","count":1.00000000000000000001}'
    if protocol == "anthropic_messages":
        valid, invalid = load_probe_json(valid), load_probe_json(invalid)
    assert validate_output(protocol, _build_response(protocol, arguments=valid)) == STOPS[protocol]
    _check_error(protocol, _build_response(protocol, arguments=invalid), "invalid_tool_arguments")


@pytest.mark.parametrize("protocol", ["openai_chat", "openai_responses"])
@pytest.mark.parametrize(
    "arguments",
    [
        '{"value":"ping","count":NaN}',
        '{"value":"ping","count":1,"count":1}',
        '{"value":"ping","count":1}{}',
        '{"value":"private-upstream-text"',
    ],
)
def test_encoded_arguments_require_one_strict_json_value(protocol, arguments):
    _check_error(protocol, _build_response(protocol, arguments=arguments), "invalid_tool_arguments")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_partial_and_refused_tool_outputs_do_not_pass(protocol):
    reply = _build_response(protocol)
    refused = deepcopy(reply)
    if protocol == "openai_chat":
        reply["choices"][0]["finish_reason"] = "length"
        refused["choices"][0]["message"]["refusal"] = "private-upstream-text"
    elif protocol == "anthropic_messages":
        reply["stop_reason"] = "max_tokens"
        refused["stop_reason"] = "refusal"
    else:
        reply["output"][0]["status"] = "incomplete"
        refused["output"].append(
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "refusal", "refusal": "private-upstream-text"}],
            }
        )
    _check_error(protocol, reply, "incomplete_response")
    _check_error(protocol, refused, "refusal")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_malformed_response_envelopes_cannot_attest_to_a_call(protocol):
    reply = _build_response(protocol)
    if protocol == "openai_chat":
        reply["choices"][0]["message"]["tool_calls"][0].pop("id")
    elif protocol == "anthropic_messages":
        reply["content"][0].pop("id")
    else:
        reply["output"][0].pop("call_id")
    _check_error(protocol, reply, "invalid_structure")


def test_native_structured_call_cannot_hide_an_unexpected_server_tool():
    response = _build_response("openai_responses")
    response["output"].append({"type": "web_search_call"})
    _check_error("openai_responses", response, "unexpected_tool_call_count")

    message = _build_response("anthropic_messages")
    message["content"].append({"type": "server_tool_use"})
    _check_error("anthropic_messages", message, "unexpected_tool_call_count")
    message["content"][-1] = {"type": "mcp_tool_use"}
    _check_error("anthropic_messages", message, "unexpected_tool_call_count")


def test_chat_valid_call_cannot_override_malformed_refusal_metadata():
    reply = _build_response("openai_chat")
    reply["choices"][0]["message"]["refusal"] = {"text": "private-upstream-text"}
    _check_error("openai_chat", reply, "invalid_structure")
