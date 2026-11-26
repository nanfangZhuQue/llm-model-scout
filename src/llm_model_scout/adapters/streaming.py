from __future__ import annotations

import json
import re
from collections import defaultdict
from collections.abc import AsyncIterable, AsyncIterator
from contextlib import aclosing
from dataclasses import dataclass

from ..domain import Protocol
from . import (
    InvalidResponse,
    anthropic_messages,
    no_text_error,
    openai_chat,
    openai_responses,
    safe_stop_reason,
)

_LINE_END = re.compile(rb"\r\n?|\n")


def _invalid(message="Invalid server-sent event") -> InvalidResponse:
    return InvalidResponse(message, "invalid_stream")


def _interrupted() -> InvalidResponse:
    return InvalidResponse("Stream ended before it completed", "stream_interrupted")


def _empty(stop=None) -> InvalidResponse:
    return InvalidResponse("Stream contained no non-empty assistant text", "empty_stream", stop)


async def _events(
    chunks: AsyncIterable[bytes], max_bytes: int
) -> AsyncIterator[tuple[str | None, str]]:
    pending = bytearray()
    data: list[str] = []
    event = None
    skip_lf = False
    first_line = True
    remaining = max_bytes
    async for chunk in chunks:
        if not chunk:
            continue
        window = chunk[:remaining]
        remaining -= len(window)
        start = int(skip_lf and window.startswith(b"\n"))
        skip_lf = False
        for end in _LINE_END.finditer(window, start):
            pending.extend(window[start : end.start()])
            try:
                line = pending.decode("utf-8-sig" if first_line else "utf-8")
            except UnicodeError:
                raise _invalid("Stream is not UTF-8") from None
            first_line = False
            pending.clear()
            start = end.end()
            skip_lf = start == len(window) and end.group() == b"\r"
            if not line:
                if data:
                    yield event, "\n".join(data)
                event = None
                data.clear()
                continue
            field, _, value = line.partition(":")
            if field == "data":
                data.append(value.removeprefix(" "))
            elif field == "event":
                event = value.removeprefix(" ")
        pending.extend(window[start:])
        if len(chunk) > len(window):
            raise InvalidResponse(
                "Stream exceeded the response size limit", "stream_limit_exceeded"
            )
    # SSE dispatch requires a blank line, including for the terminal event.
    if data or (pending and not pending.startswith(b":")):
        raise _interrupted()


def _json_data(raw: str) -> dict:
    try:
        data = json.loads(raw)
    except (ValueError, RecursionError):
        raise _invalid("Stream event did not contain valid JSON") from None
    if not isinstance(data, dict):
        raise _invalid("Expected a stream event object")
    return data


def _event_type(event: str | None, data: dict) -> str:
    kind = data.get("type")
    if not isinstance(kind, str) or event not in (None, "", "message", kind):
        raise _invalid("Stream event name does not match the event data")
    return kind


def _index(value: object) -> int:
    if type(value) is not int or value < 0:
        raise _invalid("Invalid stream item index")
    return value


def _no_text(has_other: bool, stop: str | None, token_limit: str | None = None) -> InvalidResponse:
    if has_other or (token_limit is not None and stop == token_limit):
        return no_text_error("non_text_output" if has_other else "empty_text", stop, token_limit)
    return _empty(stop)


@dataclass
class _ChatChoice:
    has_text: bool = False
    has_other: bool = False
    refused: bool = False
    finished: bool = False
    stop_reason: str | None = None

    def accept(self, choice: dict) -> None:
        delta = choice.get("delta")
        if (
            self.finished
            or not isinstance(delta, dict)
            or delta.get("role")
            not in (
                None,
                "assistant",
            )
        ):
            raise _invalid("Expected a Chat assistant delta before the choice finished")
        content = delta.get("content")
        refusal = delta.get("refusal")
        if (content is not None and not isinstance(content, str)) or (
            refusal is not None and not isinstance(refusal, str)
        ):
            raise _invalid("Invalid Chat text or refusal delta")
        self.has_text |= bool(content and content.strip())
        self.refused |= bool(refusal and refusal.strip())
        self.has_other |= any(
            delta.get(key)
            for key in (
                "tool_calls",
                "function_call",
                "reasoning",
                "reasoning_content",
                "reasoning_details",
                "audio",
            )
        )
        stop = choice.get("finish_reason")
        if stop is not None:
            if not isinstance(stop, str) or not stop.strip():
                raise _invalid("Invalid Chat finish reason")
            self.finished = True
            self.stop_reason = safe_stop_reason(stop, openai_chat.STOP_REASONS)
            self.refused |= stop == "content_filter"
            self.has_other |= stop in ("tool_calls", "function_call")


class _ChatStream:
    def __init__(self):
        self.choices: defaultdict[int, _ChatChoice] = defaultdict(_ChatChoice)

    def event(self, event: str | None, raw: str) -> tuple[bool, str | None]:
        if event == "error":
            raise InvalidResponse("Provider reported a stream error", "response_error")
        if raw == "[DONE]" and event in (None, "", "message"):
            return True, self.finish()
        payload = _json_data(raw)
        if payload.get("error") is not None:
            raise InvalidResponse("Provider reported a stream error", "response_error")
        choices = payload.get("choices")
        if payload.get(
            "object", "chat.completion.chunk"
        ) != "chat.completion.chunk" or not isinstance(choices, list):
            raise _invalid("Expected a Chat completion chunk")
        for choice in choices:
            if not isinstance(choice, dict):
                raise _invalid("Invalid Chat stream choice")
            index = _index(choice.get("index"))
            self.choices[index].accept(choice)
        return False, None

    def finish(self) -> str | None:
        if any(not choice.finished for choice in self.choices.values()):
            raise _interrupted()
        for choice in self.choices.values():
            if choice.has_text and not choice.refused:
                return choice.stop_reason
        for choice in self.choices.values():
            if choice.refused:
                raise InvalidResponse("Provider reported refusal", "refusal", choice.stop_reason)
        for choice in self.choices.values():
            if choice.stop_reason == "length" or choice.has_other:
                raise _no_text(choice.has_other, choice.stop_reason, "length")
        raise _empty(next((choice.stop_reason for choice in self.choices.values()), None))


class _MessagesStream:
    def __init__(self):
        self.started = False
        self.stop_reason: str | None = None
        self.blocks: dict[int, str | None] = {}
        self.open_blocks = 0
        self.has_text = False
        self.has_other = False

    def event(self, event: str | None, raw: str) -> tuple[bool, str | None]:
        payload = _json_data(raw)
        kind = _event_type(event, payload)
        if kind == "error":
            raise InvalidResponse("Provider reported a stream error", "response_error")
        if kind == "message_start":
            message = payload.get("message")
            if (
                self.started
                or not isinstance(message, dict)
                or message.get("type") != "message"
                or message.get("role") != "assistant"
            ):
                raise _invalid("Expected one Messages assistant message start")
            self.started = True
        elif kind in ("content_block_start", "content_block_delta", "content_block_stop"):
            if not self.started or self.stop_reason is not None:
                raise _invalid("Messages content block outside the message body")
            self.block_event(kind, payload)
        elif kind == "message_delta":
            delta = payload.get("delta")
            if not self.started or self.open_blocks or not isinstance(delta, dict):
                raise _invalid("Expected Messages delta after the content blocks closed")
            stop = delta.get("stop_reason")
            if stop is not None:
                if not isinstance(stop, str) or not stop.strip():
                    raise _invalid("Invalid Messages stop reason")
                if self.stop_reason is not None and stop != self.stop_reason:
                    raise _invalid("Conflicting Messages stop reasons")
                self.stop_reason = stop
        elif kind == "message_stop":
            if self.stop_reason is None:
                raise _invalid("Messages stream stopped before its message delta")
            stop = safe_stop_reason(self.stop_reason, anthropic_messages.STOP_REASONS)
            if stop == "refusal":
                raise InvalidResponse("Provider reported refusal", "refusal", stop)
            if not self.has_text:
                raise _no_text(self.has_other, stop, "max_tokens")
            return True, stop
        return False, None

    def block_event(self, kind: str, payload: dict) -> None:
        index = _index(payload.get("index"))
        if kind == "content_block_start":
            block = payload.get("content_block")
            if (
                index in self.blocks
                or not isinstance(block, dict)
                or not isinstance(block.get("type"), str)
            ):
                raise _invalid("Invalid Messages content block start")
            self.blocks[index] = block["type"]
            self.open_blocks += 1
            if block["type"] == "text":
                self.text(block)
            else:
                self.has_other = True
        elif self.blocks.get(index) is None:
            raise _invalid("Messages content block event without an open block")
        elif kind == "content_block_delta":
            delta = payload.get("delta")
            if not isinstance(delta, dict) or not isinstance(delta.get("type"), str):
                raise _invalid("Invalid Messages content block delta")
            if delta["type"] == "text_delta":
                if self.blocks[index] != "text":
                    raise _invalid("Messages text delta outside a text block")
                self.text(delta)
        else:
            self.open_blocks -= 1
            self.blocks[index] = None

    def text(self, data: dict) -> None:
        value = data.get("text")
        if not isinstance(value, str):
            raise _invalid("Invalid Messages text")
        self.has_text |= bool(value.strip())


class _ResponsesStream:
    def __init__(self):
        self.has_delta = False
        self.refused = False

    def event(self, event: str | None, raw: str) -> tuple[bool, str | None]:
        payload = _json_data(raw)
        kind = _event_type(event, payload)
        if kind in ("error", "response.failed"):
            raise InvalidResponse("Provider reported a stream error", "response_error", "failed")
        if kind == "response.incomplete":
            response = payload.get("response")
            details = response.get("incomplete_details") if isinstance(response, dict) else None
            reason = (
                safe_stop_reason(details.get("reason"), openai_responses.INCOMPLETE_REASONS)
                if isinstance(details, dict)
                else None
            )
            raise InvalidResponse(
                "Provider response did not complete", "incomplete_response", reason or "incomplete"
            )
        if kind in (
            "response.output_text.delta",
            "response.refusal.delta",
            "response.refusal.done",
        ):
            value = payload.get("refusal" if kind.endswith(".done") else "delta")
            if not isinstance(value, str):
                raise _invalid("Invalid Responses text or refusal event")
            if kind == "response.output_text.delta":
                self.has_delta |= bool(value.strip())
            else:
                self.refused |= bool(value.strip())
        elif kind == "response.completed":
            if self.refused:
                raise InvalidResponse("Provider reported refusal", "refusal", "completed")
            try:
                stop = openai_responses.validate_text(payload.get("response"))
            except InvalidResponse as exc:
                if exc.code == "empty_text":
                    raise _empty(exc.stop_reason) from None
                if exc.code == "invalid_structure":
                    raise _invalid("Invalid final Responses stream object") from None
                raise
            if not self.has_delta:
                raise _empty(stop)
            return True, stop
        return False, None


async def validate_stream(
    protocol: Protocol, chunks: AsyncIterable[bytes], max_bytes: int
) -> str | None:
    validator = {
        "openai_chat": _ChatStream,
        "anthropic_messages": _MessagesStream,
        "openai_responses": _ResponsesStream,
    }[protocol]()
    saw_event = False
    async with aclosing(_events(chunks, max_bytes)) as events:
        async for event, raw in events:
            if not raw and not event:
                continue
            saw_event = True
            finished, stop = validator.event(event, raw)
            if finished:
                return stop
    raise _interrupted() if saw_event else _empty()
