"""Build and inspect a single synthetic, non-streaming tool call."""

from copy import deepcopy
from dataclasses import replace

from ..domain import TOOL_PROMPT, EndpointConfig, Protocol
from . import (
    InvalidResponse,
    adapter_for,
    anthropic_messages,
    content_kind,
    openai_chat,
    openai_responses,
)
from .json_values import is_json_integer, load_probe_json

TOOL_NAME = "scout_tool_probe"
_TOOL_DESCRIPTION = "Emit synthetic values for a tool-call capability probe."
_SCHEMA = {
    "type": "object",
    "properties": {"value": {"type": "string", "enum": ["ping"]}, "count": {"type": "integer"}},
    "required": ["value", "count"],
    "additionalProperties": False,
}


def request_body(endpoint: EndpointConfig, model: str) -> dict:
    probe = replace(endpoint.probe, prompt=TOOL_PROMPT)
    body = adapter_for(endpoint).request_body(model, probe)
    schema = deepcopy(_SCHEMA)
    tool = {"name": TOOL_NAME, "description": _TOOL_DESCRIPTION, "strict": True}
    if endpoint.protocol == "openai_chat":
        body["tools"] = [{"type": "function", "function": {**tool, "parameters": schema}}]
        body["tool_choice"] = {"type": "function", "function": {"name": TOOL_NAME}}
        body["parallel_tool_calls"] = False
    elif endpoint.protocol == "openai_responses":
        body["tools"] = [{"type": "function", **tool, "parameters": schema}]
        body["tool_choice"] = {"type": "function", "name": TOOL_NAME}
        body["parallel_tool_calls"] = False
    else:
        body["tools"] = [{**tool, "input_schema": schema}]
        body["tool_choice"] = {
            "type": "tool",
            "name": TOOL_NAME,
            "disable_parallel_tool_use": True,
        }
    return body


def _single_call(calls: list, stop: str | None) -> dict:
    if not calls:
        raise InvalidResponse("Provider did not return a tool call", "no_tool_call", stop)
    if len(calls) != 1:
        raise InvalidResponse(
            "Provider returned multiple tool calls", "unexpected_tool_call_count", stop
        )
    if not isinstance(calls[0], dict):
        raise InvalidResponse("Expected a tool call object", stop_reason=stop)
    return calls[0]


def _validate_arguments(
    name: object, arguments: object, stop: str | None, *, encoded: bool
) -> None:
    if not isinstance(name, str) or not name.strip():
        raise InvalidResponse("Tool call did not contain a valid name", stop_reason=stop)
    if name != TOOL_NAME:
        raise InvalidResponse("Provider called a different tool", "unexpected_tool_name", stop)

    if encoded and isinstance(arguments, str):
        try:
            arguments = load_probe_json(arguments)
        except (ValueError, RecursionError):
            arguments = None
    elif encoded:
        arguments = None
    if (
        not isinstance(arguments, dict)
        or arguments.keys() != _SCHEMA["properties"].keys()
        or arguments["value"] not in _SCHEMA["properties"]["value"]["enum"]
        or not is_json_integer(arguments["count"])
    ):
        raise InvalidResponse(
            "Tool arguments did not match the requested schema", "invalid_tool_arguments", stop
        )


def _validate_stop(raw_stop: object, stop: str | None, incomplete: tuple[str, ...]) -> None:
    if raw_stop is not None and stop is None:
        raise InvalidResponse("Invalid stop metadata for tool call")
    if stop in incomplete:
        raise InvalidResponse("Tool call response did not complete", "incomplete_response", stop)


def _valid_id(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _validate_chat(data: object) -> str:
    choices = openai_chat.choices_from(data)
    if len(choices) != 1:
        raise InvalidResponse("Expected a single Chat completion choice")
    (choice,) = choices
    message, stop = openai_chat.choice_message(choice)
    _validate_stop(choice.get("finish_reason"), stop, ("length",))
    refusal = message.get("refusal")
    if refusal is not None and not isinstance(refusal, str):
        raise InvalidResponse("Invalid Chat assistant refusal", stop_reason=stop)
    if content_kind(message.get("content")) == "invalid_structure":
        raise InvalidResponse("Invalid Chat assistant content", stop_reason=stop)
    calls = message.get("tool_calls")
    if calls is None:
        calls = []
    if not isinstance(calls, list) or message.get("function_call") is not None:
        raise InvalidResponse("Expected Chat tool_calls array", stop_reason=stop)
    call = _single_call(calls, stop)
    if (
        stop != "tool_calls"
        or call.get("type") != "function"
        or not _valid_id(call.get("id"))
        or not isinstance(call.get("function"), dict)
    ):
        raise InvalidResponse("Invalid Chat function call envelope", stop_reason=stop)
    function = call["function"]
    _validate_arguments(function.get("name"), function.get("arguments"), stop, encoded=True)
    return stop


def _validate_anthropic(data: object) -> str:
    content, stop = anthropic_messages.message_content(data)
    _validate_stop(
        data.get("stop_reason"), stop, ("max_tokens", "pause_turn", "model_context_window_exceeded")
    )
    if content_kind(content) == "invalid_structure":
        raise InvalidResponse("Invalid Messages assistant content", stop_reason=stop)
    calls = []
    for block in content:
        kind = block["type"]
        if kind == "refusal":
            raise InvalidResponse("Provider reported refusal", "refusal", stop)
        if kind == "tool_use" or kind.endswith("_tool_use"):
            calls.append(block)
        elif kind not in ("text", "thinking", "redacted_thinking"):
            raise InvalidResponse("Unexpected Messages content block type", stop_reason=stop)
    call = _single_call(calls, stop)
    if stop != "tool_use" or call["type"] != "tool_use" or not _valid_id(call.get("id")):
        raise InvalidResponse("Invalid Messages tool use envelope", stop_reason=stop)
    _validate_arguments(call.get("name"), call.get("input"), stop, encoded=False)
    return stop


def _validate_responses(data: object) -> str:
    output, stop = openai_responses.response_output(data)
    calls = []
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise InvalidResponse("Invalid response output item", stop_reason=stop)
        if item["type"] == "message":
            openai_responses.assistant_content(item, stop)
        elif item["type"].endswith("_call"):
            calls.append(item)
        elif item["type"] != "reasoning":
            raise InvalidResponse("Unexpected Responses output item type", stop_reason=stop)

    call = _single_call(calls, stop)
    item_id = call.get("id")
    if (
        call["type"] != "function_call"
        or not _valid_id(call.get("call_id"))
        or (item_id is not None and not _valid_id(item_id))
    ):
        raise InvalidResponse("Invalid Responses function call envelope", stop_reason=stop)
    if call.get("namespace") not in (None, ""):
        raise InvalidResponse("Provider called a different tool", "unexpected_tool_name", stop)
    status = call.get("status", "completed")
    if status in ("in_progress", "incomplete"):
        raise InvalidResponse("Tool call response did not complete", "incomplete_response", status)
    if status != "completed":
        raise InvalidResponse("Invalid Responses function call status", stop_reason=stop)
    _validate_arguments(call.get("name"), call.get("arguments"), stop, encoded=True)
    return stop


def validate_output(protocol: Protocol, data: object) -> str:
    if protocol == "openai_chat":
        return _validate_chat(data)
    if protocol == "openai_responses":
        return _validate_responses(data)
    return _validate_anthropic(data)
