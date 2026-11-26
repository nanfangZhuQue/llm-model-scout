import asyncio
import time
from dataclasses import replace

import httpx

from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProviderConfig, ScanPolicy
from llm_model_scout.transport import RequestGate


async def test_two_scans_share_concurrency_budget_and_keep_local_counts():
    active = peak = requests = 0

    async def handler(request):
        nonlocal active, peak, requests
        active += 1
        peak = max(peak, active)
        requests += 1
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    endpoint = EndpointConfig(
        "e", "openai_chat", "https://mock.test/v1", "synthetic-secret", models=("a", "b", "c")
    )
    policy = ScanPolicy(concurrency=1, max_requests=5, retries=0)
    shared = RequestGate(policy)
    provider = ProviderConfig("p", (endpoint,), replace(policy, max_requests=3))
    a, b = await asyncio.gather(
        *(
            scan(provider, shared_gate=shared, http_transport=httpx.MockTransport(handler))
            for _ in range(2)
        )
    )
    assert peak == 1
    assert shared.sent == requests == a.requests_sent + b.requests_sent == 5
    assert max(a.requests_sent, b.requests_sent) <= 3


async def test_cancelling_gate_wait_does_not_consume_shared_budget():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def handler(request):
        entered.set()
        await release.wait()
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    policy = ScanPolicy(concurrency=1, max_requests=10)
    provider = ProviderConfig(
        "p",
        (EndpointConfig("e", "openai_chat", "https://mock.test/v1", "test-key", models=("a",)),),
        policy,
    )
    shared = RequestGate(policy)
    transport = httpx.MockTransport(handler)
    first = asyncio.create_task(scan(provider, shared_gate=shared, http_transport=transport))
    await entered.wait()
    second = asyncio.create_task(scan(provider, shared_gate=shared, http_transport=transport))
    await asyncio.sleep(0.02)
    second.cancel()
    report = await second
    release.set()
    await first
    assert shared.sent == 1
    assert report.requests_sent == 0


async def test_cooldown_is_shared_between_scans():
    starts = []

    def handler(request):
        starts.append(time.monotonic())
        if len(starts) == 1:
            return httpx.Response(429, headers={"retry-after": "0.06"}, json={"error": "limited"})
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
        )

    policy = ScanPolicy(concurrency=1, max_requests=10, retries=0, backoff_base=0, rate_limit=100)
    endpoint = EndpointConfig("e", "openai_chat", "https://mock.test/v1", "test-key", models=("m",))
    provider = ProviderConfig("p", (endpoint,), policy)
    gate = RequestGate(policy)
    await asyncio.gather(
        *(
            scan(provider, shared_gate=gate, http_transport=httpx.MockTransport(handler))
            for _ in range(2)
        )
    )
    assert len(starts) == 2 and starts[1] - starts[0] >= 0.05
