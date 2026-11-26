from ..domain import ProbeConfig
from . import InvalidResponse, content_kind, no_text_error, safe_stop_reason

PATH = "chat/completions"
STOP_REASONS = ("stop", "length", "tool_calls", "function_call", "content_filter")


def request_body(model: str, probe: ProbeConfig) -> dict:
    body = {
        "model": model,
        "messages": [{"role": "user", "content": probe.prompt}],
        "max_tokens": probe.max_tokens,
        "stream": False,
    }
    if probe.temperature is not None:
        body["temperature"] = probe.temperature
    return body


def choices_from(data: object) -> list:
    if not isinstance(data, dict) or "error" in data:
        raise InvalidResponse("Expected a chat completion object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise InvalidResponse("Missing non-empty choices")
    return choices


def choice_message(choice: object) -> tuple[dict, str | None]:
    if not isinstance(choice, dict) or not isinstance(choice.get("message"), dict):
        raise InvalidResponse("Missing assistant message")
    message = choice["message"]
    if message.get("role") != "assistant":
        raise InvalidResponse("Expected assistant role")
    stop = safe_stop_reason(choice.get("finish_reason"), STOP_REASONS)
    refusal = message.get("refusal")
    content = message.get("content")
    refusal_block = isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "refusal" for block in content
    )
    if stop == "content_filter" or refusal_block or (isinstance(refusal, str) and refusal.strip()):
        raise InvalidResponse("Provider reported refusal or filtering", "refusal", stop)
    return message, stop


def choice_text(choice: object) -> tuple[str | list, str | None]:
    message, stop = choice_message(choice)
    content = message.get("content")
    kind = content_kind(content)
    if kind == "text":
        return content, stop
    if kind == "empty_text" and (
        any(
            isinstance(message.get(k), list) and message[k]
            for k in ("tool_calls", "reasoning_details")
        )
        or isinstance(message.get("function_call"), dict)
        or (isinstance(message.get("reasoning"), str) and message["reasoning"].strip())
    ):
        kind = "non_text_output"
    raise no_text_error(kind, stop, "length")


def validate_text(data: object) -> str | None:
    failures = []
    for choice in choices_from(data):
        try:
            _, stop = choice_text(choice)
            return stop
        except InvalidResponse as exc:
            failures.append(exc)
    priority = (
        "refusal",
        "token_limit_without_text",
        "non_text_output",
        "empty_text",
        "invalid_structure",
    )
    raise min(failures, key=lambda error: priority.index(error.code))
