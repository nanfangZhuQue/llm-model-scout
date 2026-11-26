from __future__ import annotations

from ..domain import DEFAULT_AUTH, EndpointConfig

SUGGESTIONS = {
    "invalid_structure": "Check the endpoint protocol and provider response format.",
    "empty_text": "Cause unknown; check response format and explicit probe settings.",
    "token_limit_without_text": "Consider explicit --max-tokens increase; it may cost more.",
    "non_text_output": "Only non-text output was observed; this probe requires assistant text.",
    "refusal": "Check provider refusal/filtering policy; do not retry automatically.",
    "incomplete_response": "Check the reported completion reason and output token limit.",
    "response_error": "Check provider status and explicit probe settings.",
    "empty_stream": "Check whether the model produces assistant text with streaming enabled.",
    "stream_interrupted": "Check provider and network stability; the stream did not finish.",
    "stream_limit_exceeded": "Check the output token limit and the configured response size limit.",
    "invalid_stream": "Check that the endpoint emits server-sent events for this protocol.",
    "invalid_structured_json": "Check that the model returns a single JSON object without markup.",
    "schema_mismatch": "Check support for the requested JSON schema and output field types.",
    "no_tool_call": "Check whether this endpoint emits structured calls for the requested tool.",
    "unexpected_tool_name": "Check whether the model honors the requested tool name.",
    "invalid_tool_arguments": "Check that the tool arguments satisfy the requested schema.",
    "unexpected_tool_call_count": "Check that the model returns only one tool call.",
}


class InvalidResponse(ValueError):
    def __init__(self, message: str, code: str = "invalid_structure", stop_reason=None):
        super().__init__(message)
        self.code = code
        self.stop_reason = stop_reason
        self.suggestion = SUGGESTIONS[code]


def safe_stop_reason(value: object, allowed: tuple[str, ...]) -> str | None:
    return value if isinstance(value, str) and value in allowed else None


def content_kind(content: object) -> str:
    if content is None:
        return "empty_text"
    if isinstance(content, str):
        return "text" if content.strip() else "empty_text"
    if not isinstance(content, list):
        return "invalid_structure"
    has_text = has_other = False
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            return "invalid_structure"
        if block["type"] == "text":
            if not isinstance(block.get("text"), str):
                return "invalid_structure"
            has_text |= bool(block["text"].strip())
        else:
            has_other = True
    if has_text:
        return "text"
    return "non_text_output" if has_other else "empty_text"


def no_text_error(kind: str, stop: str | None, token_limit: str | None = None) -> InvalidResponse:
    if kind == "invalid_structure":
        return InvalidResponse("Invalid assistant content structure", stop_reason=stop)
    if token_limit is not None and stop == token_limit:
        return InvalidResponse(
            "Token limit reported without non-empty assistant text",
            "token_limit_without_text",
            stop,
        )
    message = "No non-empty assistant text" if kind == "empty_text" else "Only non-text output"
    return InvalidResponse(message, kind, stop)


def headers(endpoint: EndpointConfig) -> dict[str, str]:
    auth = endpoint.auth
    if auth == "default":
        auth = DEFAULT_AUTH[endpoint.protocol]
    result = {"Content-Type": "application/json", "Accept": "application/json"}
    if auth == "bearer":
        result["Authorization"] = f"Bearer {endpoint.api_key}"
    else:
        result["x-api-key"] = endpoint.api_key
    if endpoint.protocol == "anthropic_messages":
        result["anthropic-version"] = endpoint.api_version
    return result


def models_page(data: object) -> tuple[list[str], str | None]:
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("data"), list):
        raise InvalidResponse("Model list must contain a data array")
    ids = []
    for row in data["data"]:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"].strip():
            raise InvalidResponse("Model list contains an invalid id")
        ids.append(row["id"])
    has_more = data.get("has_more", False)
    if not isinstance(has_more, bool):
        raise InvalidResponse("Invalid pagination flag")
    cursor = None
    if has_more:
        cursor = data.get("last_id") or (ids[-1] if ids else None)
        if not isinstance(cursor, str) or not cursor:
            raise InvalidResponse("Pagination has_more without a cursor")
    return ids, cursor


def adapter_for(endpoint: EndpointConfig):
    from . import anthropic_messages, openai_chat, openai_responses

    return {
        "openai_chat": openai_chat,
        "anthropic_messages": anthropic_messages,
        "openai_responses": openai_responses,
    }[endpoint.protocol]
