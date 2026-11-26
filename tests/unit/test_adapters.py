from dataclasses import replace

import pytest

from llm_model_scout.adapters import InvalidResponse, adapter_for, headers, models_page
from llm_model_scout.adapters import anthropic_messages as ant
from llm_model_scout.adapters import openai_chat as oai
from llm_model_scout.adapters import openai_responses as responses
from llm_model_scout.domain import EndpointConfig, ProbeConfig


@pytest.mark.parametrize(
    "adapter,data",
    [
        (oai, {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}),
        (
            oai,
            {
                "choices": [
                    {"message": {"role": "assistant", "content": [{"type": "text", "text": "ok"}]}}
                ]
            },
        ),
        (
            ant,
            {"type": "message", "role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ),
    ],
)
def test_text_success(adapter, data):
    adapter.validate_text(data)


@pytest.mark.parametrize(
    "data",
    [
        None,
        {},
        [],
        {"error": "failed"},
        {"choices": []},
        {"choices": [{"message": {"role": "assistant", "content": " "}}]},
        {"choices": [{"message": {"role": "user", "content": "ping"}}]},
        {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "thinking", "thinking": "x"}],
        },
    ],
)
@pytest.mark.parametrize("adapter", [oai, ant])
def test_bad_response(adapter, data):
    with pytest.raises(InvalidResponse):
        adapter.validate_text(data)


def test_auth_isolation_and_profiles():
    endpoint = EndpointConfig("oai", "openai_chat", "https://example.test/v1", "test-secret")
    assert "Authorization" in headers(endpoint)
    assert "x-api-key" not in headers(endpoint)
    anthropic = replace(endpoint, protocol="anthropic_messages")
    assert "Authorization" not in headers(anthropic)
    assert headers(anthropic)["anthropic-version"] == "2023-06-01"
    assert "temperature" not in ant.request_body("any", ProbeConfig.named("chat-basic"))
    assert oai.request_body("any", ProbeConfig())["max_tokens"] == 1
    assert "fallbacks" not in ant.request_body("any", ProbeConfig())


def test_models_pagination():
    assert models_page({"data": [{"id": "X"}], "has_more": True}) == (["X"], "X")
    with pytest.raises(InvalidResponse):
        models_page({"data": [], "has_more": True})


def responses_message(*content, status="completed", role="assistant"):
    return {"type": "message", "role": role, "status": status, "content": list(content)}


def test_responses_request_and_auth():
    endpoint = EndpointConfig("responses", "openai_responses", "https://example.test/v1", "key")
    assert adapter_for(endpoint) is responses
    assert responses.PATH == "responses"
    assert responses.request_body("example", ProbeConfig.named("chat-basic")) == {
        "model": "example",
        "input": "ping",
        "max_output_tokens": 16,
        "stream": False,
        "store": False,
    }
    assert responses.request_body("example", ProbeConfig()) == {
        "model": "example",
        "input": "ping",
        "max_output_tokens": 1,
        "stream": False,
        "store": False,
        "temperature": 0,
    }
    assert headers(endpoint)["Authorization"] == "Bearer key"
    assert "x-api-key" not in headers(endpoint)
    custom_auth = headers(replace(endpoint, auth="x-api-key"))
    assert custom_auth["x-api-key"] == "key"
    assert "Authorization" not in custom_auth
    assert "anthropic-version" not in custom_auth


def test_responses_accepts_completed_assistant_text_after_reasoning():
    message = responses_message({"type": "output_text", "text": "pong"})
    payload = {
        "object": "response",
        "status": "completed",
        "error": None,
        "incomplete_details": None,
        "output": [{"type": "reasoning"}, message],
    }
    assert responses.validate_text(payload) == "completed"
    del message["status"]
    assert responses.validate_text(payload) == "completed"


@pytest.mark.parametrize(
    "output,code,stop",
    [
        ([], "empty_text", "completed"),
        ([responses_message({"type": "output_text", "text": " \n"})], "empty_text", "completed"),
        ([{"type": "reasoning", "text": "internal"}], "non_text_output", "completed"),
        ([{"type": "function_call", "name": "fake_tool"}], "non_text_output", "completed"),
        (
            [responses_message({"type": "output_text", "text": None})],
            "invalid_structure",
            "completed",
        ),
        (
            [responses_message({"type": "output_text", "text": "echo"}, role="user")],
            "invalid_structure",
            "completed",
        ),
        (
            [responses_message({"type": "output_text", "text": "partial"}, status="incomplete")],
            "incomplete_response",
            "incomplete",
        ),
        (
            [
                responses_message({"type": "output_text", "text": "mixed text"}),
                responses_message({"type": "refusal", "refusal": "private refusal"}),
            ],
            "refusal",
            "completed",
        ),
    ],
)
def test_responses_requires_completed_nonempty_assistant_text(output, code, stop):
    with pytest.raises(InvalidResponse) as exc:
        responses.validate_text(
            {"object": "response", "status": "completed", "output": output, "output_text": "fake"}
        )
    assert exc.value.code == code
    assert exc.value.stop_reason == stop
    assert "private refusal" not in str(exc.value)


@pytest.mark.parametrize(
    "status,extra,code,stop",
    [
        (
            "incomplete",
            {"incomplete_details": {"reason": "max_output_tokens"}},
            "incomplete_response",
            "max_output_tokens",
        ),
        (
            "incomplete",
            {"incomplete_details": {"reason": "private reason"}},
            "incomplete_response",
            "incomplete",
        ),
        ("queued", {}, "incomplete_response", "queued"),
        ("in_progress", {}, "incomplete_response", "in_progress"),
        ("cancelled", {}, "incomplete_response", "cancelled"),
        ("failed", {"error": {"message": "private error"}}, "response_error", "failed"),
        ("completed", {"error": {"message": "private error"}}, "response_error", "failed"),
        (None, {}, "invalid_structure", None),
    ],
)
def test_responses_rejects_unfinished_or_error_status_even_with_text(status, extra, code, stop):
    payload = {
        "object": "response",
        "status": status,
        "output": [responses_message({"type": "output_text", "text": "partial"})],
        **extra,
    }
    with pytest.raises(InvalidResponse) as exc:
        responses.validate_text(payload)
    assert exc.value.code == code
    assert exc.value.stop_reason == stop
    assert "private" not in str(exc.value)
