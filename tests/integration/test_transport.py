import asyncio
import time
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

import httpx

from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProviderConfig, ScanPolicy
from llm_model_scout.transport import retry_after_seconds

GOOD = {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}


def setup(**policy):
    endpoint = EndpointConfig(
        "test", "openai_chat", "https://example.test/v1", "synthetic-key", models=("a", "b", "c")
    )
    return ProviderConfig("test", (endpoint,), ScanPolicy(backoff_base=0, **policy))


def test_retry_after_parsing():
    assert retry_after_seconds("0.25") == 0.25
    assert retry_after_seconds("NaN") == 0
    assert retry_after_seconds("inf") == 0
    assert retry_after_seconds("invalid") == 0
    assert retry_after_seconds("-1") == 0
    future = format_datetime(datetime.now(UTC) + timedelta(seconds=5))
    assert 3 < retry_after_seconds(future) <= 5


async def test_shared_cooldown():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        if len(starts) == 1:
            return httpx.Response(429, headers={"retry-after": "0.05"}, json={"error": "limited"})
        return httpx.Response(200, json=GOOD)

    report = await scan(setup(concurrency=1), http_transport=httpx.MockTransport(handler))
    assert starts[1] - starts[0] >= 0.045
    assert all(r.status == "available" for r in report.results)


async def test_rate_limit():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        return httpx.Response(200, json=GOOD)

    await scan(setup(rate_limit=30), http_transport=httpx.MockTransport(handler))
    assert all(b - a >= 0.028 for a, b in zip(starts, starts[1:], strict=False))


async def test_exhausted_budget_does_not_sleep_for_retry_after():
    handler = httpx.MockTransport(
        lambda r: httpx.Response(429, headers={"retry-after": "86400"}, json={"error": "limited"})
    )
    report = await asyncio.wait_for(scan(setup(max_requests=1), http_transport=handler), 2)
    assert report.requests_sent == 1
    assert report.results[0].reason == "retry_budget_exhausted"
    assert report.status == "failed"


async def test_known_401_with_stalled_body_is_not_retried():
    class SlowBody(httpx.AsyncByteStream):
        async def __aiter__(self):
            await asyncio.sleep(1)
            yield b"{}"

    count = 0

    def handler(request):
        nonlocal count
        count += 1
        return httpx.Response(401, stream=SlowBody())

    report = await scan(setup(timeout=0.02), http_transport=httpx.MockTransport(handler))
    assert count == 3  # one attempt per model, no retries after a known 401
    assert all(r.attempts == 1 for r in report.results)


async def test_deep_json_is_bad_response_not_scan_crash():
    body = b"[" * 2000 + b"0" + b"]" * 2000
    report = await scan(
        setup(), http_transport=httpx.MockTransport(lambda r: httpx.Response(200, content=body))
    )
    assert all(r.error_category == "BAD_RESPONSE" for r in report.results)
