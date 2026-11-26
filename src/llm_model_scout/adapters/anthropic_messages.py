from ..domain import ProbeConfig
from . import InvalidResponse, content_kind, no_text_error, safe_stop_reason

PATH = "messages"
STOP_REASONS = (
    "end_turn",
    "max_tokens",
    "stop_sequence",
    "tool_use",
    "pause_turn",
    "refusal",
    "model_context_window_exceeded",
)


def request_body(model: str, probe: ProbeConfig) -> dict:
    body = {
        "model": model,
        "max_tokens": probe.max_tokens,
        "messages": [{"role": "user", "content": probe.prompt}],
        "stream": False,
    }
    if probe.temperature is not None:
        body["temperature"] = probe.temperature
    return body


def message_content(data: object) -> tuple[list, str | None]:
    if (
        not isinstance(data, dict)
        or "error" in data
        or data.get("type") != "message"
        or data.get("role") != "assistant"
        or not isinstance(data.get("content"), list)
    ):
        raise InvalidResponse("Expected an Anthropic assistant message with content blocks")
    stop = safe_stop_reason(data.get("stop_reason"), STOP_REASONS)
    if stop == "refusal":
        raise InvalidResponse("Provider reported refusal", "refusal", stop)
    return data["content"], stop


def validate_text(data: object) -> str | None:
    content, stop = message_content(data)
    kind = content_kind(content)
    if kind == "text":
        return stop
    raise no_text_error(kind, stop, "max_tokens")
