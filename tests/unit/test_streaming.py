import asyncio
import json

import pytest

from llm_model_scout.adapters import InvalidResponse
from llm_model_scout.adapters.streaming import validate_stream


def sse(data, event=None):
    name = f"event: {event}\n" if event else ""
    payload = data if isinstance(data, str) else json.dumps(data, ensure_ascii=False)
    return f"{name}data: {payload}\n\n".encode()


def named(kind, **data):
    return sse({"type": kind, **data}, kind)


def chat_chunk(index=0, stop=None, **delta):
    return sse({"choices": [{"index": index, "delta": delta, "finish_reason": stop}]})


def chat(text):
    return (
        chat_chunk(role="assistant") + chat_chunk(content=text) + chat_chunk(stop="stop"),
        sse("[DONE]"),
        "stop",
    )


def messages(text):
    start = named("message_start", message={"type": "message", "role": "assistant"})
    return (
        start
        + named("content_block_start", index=0, content_block={"type": "text", "text": ""})
        + named("content_block_delta", index=0, delta={"type": "text_delta", "text": text})
        + named("content_block_stop", index=0)
        + named("message_delta", delta={"stop_reason": "end_turn"}),
        named("message_stop"),
        "end_turn",
    )


def responses(text):
    response = {
        "object": "response",
        "status": "completed",
        "output": [
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
    }
    return (
        named("response.created") + named("response.output_text.delta", delta=text),
        named("response.completed", response=response),
        "completed",
    )


CASES = (
    ("openai_chat", chat),
    ("anthropic_messages", messages),
    ("openai_responses", responses),
)


async def chunks(body, width=7):
    for start in range(0, len(body), width):
        yield body[start : start + width]


@pytest.mark.parametrize("protocol, make", CASES)
@pytest.mark.parametrize("eol", [b"\r\n", b"\r", b"\n"])
async def test_stream_accepts_split_utf8_and_sse_line_endings(protocol, make, eol):
    prefix, terminal, stop = make("模型🍀")
    body = b"\xef\xbb\xbf: comment\n\n" + prefix + terminal
    body = body.replace(b"\n", eol)
    assert await validate_stream(protocol, chunks(body, 1), len(body)) == stop


@pytest.mark.parametrize("protocol, make", CASES)
async def test_stream_waits_for_terminal_after_text(protocol, make):
    prefix, terminal, stop = make("pong")
    waiting = asyncio.Event()
    complete = asyncio.Event()

    async def paced():
        yield prefix
        waiting.set()
        await complete.wait()
        yield terminal
        pytest.fail("Bytes after the terminal event should not be read")

    task = asyncio.create_task(validate_stream(protocol, paced(), 10_000))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        assert not task.done()
        complete.set()
        assert await asyncio.wait_for(task, timeout=1) == stop
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("protocol, make", CASES)
async def test_stream_missing_or_partial_terminal_and_empty_text(protocol, make):
    prefix, terminal, _ = make("upstream text that must not appear in errors")
    for body in (prefix, prefix + terminal[:-2]):
        with pytest.raises(InvalidResponse) as interrupted:
            await validate_stream(protocol, chunks(body), 10_000)
        assert interrupted.value.code == "stream_interrupted"
        assert "upstream text" not in str(interrupted.value)

    prefix, terminal, _ = make(" \t\n")
    for body in (b"", b": comments are not content\n\n", prefix + terminal):
        with pytest.raises(InvalidResponse) as empty:
            await validate_stream(protocol, chunks(body), 10_000)
        assert empty.value.code == "empty_stream"


@pytest.mark.parametrize("width", [1, 7, 10_000])
async def test_size_limit_counts_comments_and_terminal_frame_independent_of_chunks(width):
    prefix, terminal, _ = chat("pong")
    body = b": " + b"x" * 100 + b"\n\n" + prefix + terminal
    with pytest.raises(InvalidResponse) as limit:
        await validate_stream("openai_chat", chunks(body, width), len(body) - 1)
    assert limit.value.code == "stream_limit_exceeded"
    assert await validate_stream("openai_chat", chunks(body, width), len(body)) == "stop"


async def test_chat_supports_multiline_event_and_does_not_join_choices():
    data = b'data: {"choices": [\ndata: {"index": 0, "delta": {"content": "pong"}}]}\n\n'
    data += chat_chunk(1, stop="stop")
    with pytest.raises(InvalidResponse) as interrupted:
        await validate_stream("openai_chat", chunks(data + sse("[DONE]")), 1000)
    assert interrupted.value.code == "stream_interrupted"
    data += chat_chunk(stop="stop") + sse({"choices": []}) + sse("[DONE]")
    assert await validate_stream("openai_chat", chunks(data), 1000) == "stop"


async def test_messages_cannot_overwrite_a_refusal_with_a_conflicting_stop_reason():
    prefix, terminal, _ = messages("pong")
    repeated = named("message_delta", delta={"stop_reason": "end_turn"})
    assert (
        await validate_stream("anthropic_messages", chunks(prefix + repeated + terminal), 10_000)
        == "end_turn"
    )
    refused = prefix.replace(b'"end_turn"', b'"refusal"')
    with pytest.raises(InvalidResponse) as error:
        await validate_stream("anthropic_messages", chunks(refused + repeated + terminal), 10_000)
    assert error.value.code == "invalid_stream"


@pytest.mark.parametrize(
    "protocol, body, code, stop",
    [
        (
            "openai_chat",
            chat_chunk(content="text") + chat_chunk(stop="content_filter") + sse("[DONE]"),
            "refusal",
            "content_filter",
        ),
        (
            "openai_chat",
            chat_chunk(reasoning="reasoning-only") + chat_chunk(stop="length") + sse("[DONE]"),
            "token_limit_without_text",
            "length",
        ),
        (
            "anthropic_messages",
            named("message_start", message={"type": "message", "role": "assistant"})
            + named("content_block_start", index=0, content_block={"type": "thinking"})
            + named("content_block_stop", index=0)
            + named("message_delta", delta={"stop_reason": "end_turn"})
            + named("message_stop"),
            "non_text_output",
            "end_turn",
        ),
        (
            "openai_responses",
            named("response.output_text.delta", delta="partial")
            + named(
                "response.incomplete",
                response={"incomplete_details": {"reason": "max_output_tokens"}},
            ),
            "incomplete_response",
            "max_output_tokens",
        ),
        (
            "openai_responses",
            named("response.refusal.delta", delta="do not expose this") + responses("text")[1],
            "refusal",
            "completed",
        ),
        (
            "openai_responses",
            responses("text")[1],
            "empty_stream",
            "completed",
        ),
        (
            "openai_responses",
            named("response.output_text.delta", delta="text")
            + named("response.output_text.done", text="text"),
            "stream_interrupted",
            None,
        ),
    ],
)
async def test_stream_preserves_protocol_stop_diagnostics(protocol, body, code, stop):
    with pytest.raises(InvalidResponse) as error:
        await validate_stream(protocol, chunks(body), 10_000)
    assert error.value.code == code
    assert error.value.stop_reason == stop
    assert "do not expose this" not in str(error.value)


@pytest.mark.parametrize(
    "protocol, body",
    [
        ("openai_chat", b"data: \xff\n\n"),
        ("openai_chat", sse("{broken-json")),
        ("openai_chat", sse({"choices": [{"index": 0, "delta": {"role": "user"}}]})),
        ("anthropic_messages", sse({"type": "ping"}, "message_stop")),
        ("anthropic_messages", named("content_block_delta", index=0)),
        ("openai_responses", named("response.output_text.delta", delta=["not text"])),
        ("openai_responses", named("response.completed", response={"status": "completed"})),
    ],
)
async def test_malformed_stream_frame_fails_safely(protocol, body):
    with pytest.raises(InvalidResponse) as invalid:
        await validate_stream(protocol, chunks(body), 1000)
    assert invalid.value.code == "invalid_stream"
    assert "broken-json" not in str(invalid.value)


@pytest.mark.parametrize("protocol, make", CASES)
async def test_stream_provider_error_uses_fixed_diagnostic(protocol, make):
    prefix, _, _ = make("text")
    body = prefix + named("error", error={"message": "upstream sensitive error"})
    with pytest.raises(InvalidResponse) as invalid:
        await validate_stream(protocol, chunks(body), 1000)
    assert invalid.value.code == "response_error"
    assert "sensitive" not in str(invalid.value)
