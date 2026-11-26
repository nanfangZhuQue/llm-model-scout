import asyncio
import json
from dataclasses import replace

import httpx
import pytest

from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProviderConfig, ScanPolicy
from llm_model_scout.report import report_json

KEY = "synthetic-secret-123"
GOOD = {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}


def provider(models=("good",), **policy):
    ep = EndpointConfig("openai", "openai_chat", "https://mock.test/v1", KEY, models=models)
    return ProviderConfig("test", (ep,), ScanPolicy(backoff_base=0, **policy))


async def test_live_http_both_protocols(http_server):
    base, requests = http_server
    endpoints = (
        EndpointConfig("openai", "openai_chat", base + "/openai/v1", KEY),
        EndpointConfig("anthropic", "anthropic_messages", base + "/anthropic/v1", KEY),
    )
    report = await scan(ProviderConfig("local", endpoints))
    assert report.status == "completed"
    assert len(report.results) == 4
    assert [r.status for r in report.results] == ["unavailable", "available"] * 2
    for _, path, headers, body in requests:
        if "/anthropic/" in path:
            assert headers["x-api-key"] == KEY
            assert "Authorization" not in headers
        else:
            assert headers["Authorization"] == f"Bearer {KEY}"
            assert "x-api-key" not in headers
        if body:
            assert body["max_tokens"] == 1
    assert KEY not in report_json(report)


async def test_scan_announces_filtered_plan_before_verification():
    events = []
    first = replace(provider(("keep-a", "drop-a")).endpoints[0], api_key="a")
    second = replace(
        first,
        id="anthropic",
        protocol="anthropic_messages",
        models=("keep-b", "keep-c", "drop-b"),
    )

    def handler(request):
        assert events[2] == {"event": "planned", "count": 3}
        if request.url.path.endswith("/messages"):
            return httpx.Response(403, json={"error": {"message": "Access denied"}})
        return httpx.Response(200, json=GOOD)

    report = await scan(
        ProviderConfig("test", (first, second), ScanPolicy(retries=0)),
        include=r"^keep-",
        on_event=events.append,
        http_transport=httpx.MockTransport(handler),
    )
    assert [ev["event"] for ev in events] == [
        "discovered",
        "discovered",
        "planned",
        *["result"] * 3,
    ]
    assert [ev["count"] for ev in events[:3]] == [2, 3, 3]
    assert sorted(ev["status"] for ev in events[3:]) == ["available", "unavailable", "unavailable"]
    assert report.status == "completed" and report.requests_sent == 3


@pytest.mark.parametrize(
    "status,expected", [(401, 1), (403, 1), (404, 1), (402, 1), (429, 3), (500, 3), (529, 3)]
)
async def test_retry_policy(status, expected):
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(status, json={"error": {"message": f"failure {KEY}"}})

    report = await scan(provider(), http_transport=httpx.MockTransport(handler))
    assert len(calls) == expected
    assert report.results[0].attempts == expected
    assert KEY not in report_json(report)


async def test_budget_and_concurrency():
    active = peak = calls = 0

    async def handler(request):
        nonlocal active, peak, calls
        calls += 1
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return httpx.Response(200, json=GOOD)

    report = await scan(
        provider(tuple(str(i) for i in range(12)), concurrency=2, max_requests=5),
        http_transport=httpx.MockTransport(handler),
    )
    assert peak <= 2
    assert calls == report.requests_sent == 5
    assert sum(r.status == "skipped" for r in report.results) == 7
    assert report.status == "failed"


async def test_absolute_timeout():
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json=GOOD)

    report = await scan(provider(timeout=0.02), http_transport=httpx.MockTransport(handler))
    assert report.results[0].error_category == "TIMEOUT"
    assert report.results[0].attempts == 3


async def test_cancel_returns_partial_report():
    started = asyncio.Event()

    async def handler(request):
        started.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(
        scan(provider(("a", "b", "c"), concurrency=1), http_transport=httpx.MockTransport(handler))
    )
    await started.wait()
    task.cancel()
    report = await asyncio.wait_for(task, 2)
    assert report.status == "cancelled"
    assert [r.status for r in report.results] == ["cancelled", "skipped", "skipped"]
    assert report.results[0].attempts == 1


async def test_global_deadline():
    async def handler(request):
        await asyncio.sleep(0.2)
        return httpx.Response(200, json=GOOD)

    report = await scan(provider(scan_timeout=0.03), http_transport=httpx.MockTransport(handler))
    assert report.status == "failed"
    assert report.reason == "scan_deadline_exceeded"
    assert report.results[0].status == "cancelled"


async def test_discovery_pagination_and_partial_failure():
    requests = []

    def handler(request):
        requests.append(request)
        if request.method == "GET":
            if request.url.params:
                return httpx.Response(404, json={"error": {"message": "No page"}})
            return httpx.Response(200, json={"data": [{"id": "good"}], "has_more": True})
        return httpx.Response(200, json=GOOD)

    report = await scan(provider(None), http_transport=httpx.MockTransport(handler))
    assert report.discovery[0].status == "partial"
    assert report.status == "failed"
    assert report.results[0].status == "available"
    assert requests[1].url.params["after"] == "good"


async def test_manual_skips_discovery_and_protocol_results_are_independent():
    p = provider()
    ep = replace(p.endpoints[0], id="anthropic", protocol="anthropic_messages")
    p = replace(p, endpoints=(*p.endpoints, ep))

    def handler(request):
        assert request.method == "POST"
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"type": "message", "role": "assistant", "content": []})
        return httpx.Response(200, json=GOOD)

    report = await scan(p, http_transport=httpx.MockTransport(handler))
    assert report.results[0].status == "unavailable"
    assert report.results[1].status == "available"
    assert report.requests_sent == 2


async def test_large_and_malformed_responses():
    for body in (b"<html>login</html>", b"x" * 100):
        p = provider()
        p = replace(p, policy=replace(p.policy, max_response_bytes=50))
        report = await scan(
            p,
            http_transport=httpx.MockTransport(
                lambda r, content=body: httpx.Response(200, content=content)
            ),
        )
        assert report.results[0].error_category == "BAD_RESPONSE"


async def test_no_redirect_or_credential_forwarding():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(302, headers={"location": "https://other.test/"})

    report = await scan(provider(), http_transport=httpx.MockTransport(handler))
    assert len(calls) == 1
    assert report.results[0].status == "unavailable"


async def test_callback_failure_does_not_deadlock():
    async def event(value):
        if value["event"] == "result":
            raise ValueError("test callback failure")

    with pytest.raises(ValueError, match="callback"):
        await asyncio.wait_for(
            scan(
                provider(tuple(str(i) for i in range(20)), concurrency=1),
                on_event=event,
                http_transport=httpx.MockTransport(lambda r: httpx.Response(200, json=GOOD)),
            ),
            2,
        )


async def test_nonempty_json_error_is_not_success():
    report = await scan(
        provider(),
        http_transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json={"error": {"message": "hello"}, **GOOD})
        ),
    )
    assert json.loads(report_json(report))["results"][0]["status"] == "unavailable"
