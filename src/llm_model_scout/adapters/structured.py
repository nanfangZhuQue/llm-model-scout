"""Protocol-specific envelopes for the fixed, non-streaming structured probe."""

from copy import deepcopy
from dataclasses import replace

from ..domain import STRUCTURED_PROMPT, EndpointConfig, Protocol
from . import InvalidResponse, adapter_for, anthropic_messages, openai_chat, openai_responses
from .json_values import is_json_integer, load_probe_json

SCHEMA_NAME = "scout_structured_v1"
_SCHEMA = {
    "type": "object",
    "properties": {"status": {"type": "string", "enum": ["ok"]}, "count": {"type": "integer"}},
    "required": ["status", "count"],
    "additionalProperties": False,
}
_CHAT_ERROR_PRIORITY = (
    "refusal",
    "incomplete_response",
    "schema_mismatch",
    "invalid_structured_json",
    "token_limit_without_text",
    "non_text_output",
    "empty_text",
    "invalid_structure",
)


def request_body(endpoint: EndpointConfig, model: str) -> dict:
    probe = replace(endpoint.probe, prompt=STRUCTURED_PROMPT)
    body = adapter_for(endpoint).request_body(model, probe)
    schema = deepcopy(_SCHEMA)
    if endpoint.protocol == "openai_chat":
        body["response_format"] = {
            "type": "json_schema",
            "json_schema": {"name": SCHEMA_NAME, "strict": True, "schema": schema},
        }
    elif endpoint.protocol == "openai_responses":
        body["text"] = {
            "format": {"type": "json_schema", "name": SCHEMA_NAME, "strict": True, "schema": schema}
        }
    else:
        body["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    return body


def _validate_json(text: str, stop: str | None):
    try:
        value = load_probe_json(text)
    except (ValueError, RecursionError):
        raise InvalidResponse(
            "Output did not contain one valid JSON value", "invalid_structured_json", stop
        ) from None

    if isinstance(value, dict) and value.keys() == _SCHEMA["properties"].keys():
        count = value["count"]
        if value["status"] in _SCHEMA["properties"]["status"]["enum"] and is_json_integer(count):
            return
    raise InvalidResponse("JSON output did not match the fixed schema", "schema_mismatch", stop)


def _text_blocks(content: list, block_type: str) -> str:
    return "".join(block["text"] for block in content if block["type"] == block_type)


def _check_stop(raw_stop: object, stop: str | None, incomplete: tuple[str, ...]):
    if raw_stop is not None and stop is None:
        raise InvalidResponse("Invalid structured response stop reason")
    if stop in incomplete:
        raise InvalidResponse("Structured response did not complete", "incomplete_response", stop)


def _validate_chat(data: object) -> str | None:
    failures = []
    for choice in openai_chat.choices_from(data):
        try:
            content, stop = openai_chat.choice_text(choice)
            _check_stop(
                choice.get("finish_reason"), stop, ("length", "tool_calls", "function_call")
            )
            text = content if isinstance(content, str) else _text_blocks(content, "text")
            _validate_json(text, stop)
        except InvalidResponse as exc:
            failures.append(exc)
        else:
            return stop
    raise min(failures, key=lambda error: _CHAT_ERROR_PRIORITY.index(error.code))


def validate_output(protocol: Protocol, data: object) -> str | None:
    if protocol == "openai_chat":
        return _validate_chat(data)
    if protocol == "anthropic_messages":
        stop = anthropic_messages.validate_text(data)
        _check_stop(
            data.get("stop_reason"),
            stop,
            ("max_tokens", "tool_use", "pause_turn", "model_context_window_exceeded"),
        )
        text = _text_blocks(data["content"], "text")
    else:
        stop = openai_responses.validate_text(data)
        text = "".join(
            _text_blocks(item["content"], "output_text")
            for item in data["output"]
            if item["type"] == "message"
        )
    _validate_json(text, stop)
    return stop
