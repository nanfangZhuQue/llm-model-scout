from ..domain import ProbeConfig
from . import InvalidResponse, no_text_error, safe_stop_reason

PATH = "responses"
RESPONSE_STATUSES = ("completed", "incomplete", "in_progress", "cancelled", "queued")
INCOMPLETE_REASONS = ("max_output_tokens", "max_messages", "content_filter", "steered")


def request_body(model: str, probe: ProbeConfig) -> dict:
    body = {
        "model": model,
        "input": probe.prompt,
        "max_output_tokens": probe.max_tokens,
        "stream": False,
        "store": False,
    }
    if probe.temperature is not None:
        body["temperature"] = probe.temperature
    return body


def response_output(data: object) -> tuple[list, str]:
    if not isinstance(data, dict):
        raise InvalidResponse("Expected a Responses API response object")
    if data.get("error") is not None or data.get("status") == "failed":
        raise InvalidResponse("Provider reported a response error", "response_error", "failed")
    if data.get("object") != "response":
        raise InvalidResponse("Expected a Responses API response object")

    status = safe_stop_reason(data.get("status"), RESPONSE_STATUSES)
    if status is None:
        raise InvalidResponse("Missing or invalid response status")
    if status != "completed":
        details = data.get("incomplete_details")
        if status == "incomplete" and isinstance(details, dict):
            status = safe_stop_reason(details.get("reason"), INCOMPLETE_REASONS) or status
        raise InvalidResponse("Provider response did not complete", "incomplete_response", status)

    output = data.get("output")
    if not isinstance(output, list):
        raise InvalidResponse("Expected a response output array", stop_reason=status)
    return output, status


def assistant_content(item: dict, status: str) -> list:
    content = item.get("content")
    if item.get("role") != "assistant" or not isinstance(content, list):
        raise InvalidResponse("Expected assistant message content", stop_reason=status)
    item_status = item.get("status", "completed")
    if item_status in ("in_progress", "incomplete"):
        raise InvalidResponse(
            "Provider assistant message did not complete", "incomplete_response", item_status
        )
    if item_status != "completed":
        raise InvalidResponse("Invalid assistant message status", stop_reason=status)
    for block in content:
        if not isinstance(block, dict) or not isinstance(block.get("type"), str):
            raise InvalidResponse("Invalid assistant content block", stop_reason=status)
        if block["type"] == "refusal":
            if not isinstance(block.get("refusal"), str):
                raise InvalidResponse("Invalid assistant refusal block", stop_reason=status)
            raise InvalidResponse("Provider reported refusal", "refusal", status)
        if block["type"] == "output_text" and not isinstance(block.get("text"), str):
            raise InvalidResponse("Invalid assistant text block", stop_reason=status)
    return content


def validate_text(data: object) -> str:
    output, status = response_output(data)

    has_text = has_other = False
    for item in output:
        if not isinstance(item, dict) or not isinstance(item.get("type"), str):
            raise InvalidResponse("Invalid response output item", stop_reason=status)
        if item["type"] != "message":
            has_other = True
            continue
        for block in assistant_content(item, status):
            if block["type"] == "output_text":
                has_text |= bool(block["text"].strip())
            else:
                has_other = True

    if has_text:
        return status
    raise no_text_error("non_text_output" if has_other else "empty_text", status)
