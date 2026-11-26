import asyncio
import csv
import io
import json
import re
from contextlib import asynccontextmanager
from threading import Event
from urllib.parse import unquote

import httpx
import pytest
from openpyxl import load_workbook

from llm_model_scout.agent.app import create_app
from llm_model_scout.agent.history import HistoryError, HistoryStore
from llm_model_scout.domain import EndpointConfig, ProviderConfig, ScanPolicy
from llm_model_scout.report import CSV_FIELD_LABELS

TOKEN = "synthetic-agent-token-" + "x" * 32
KEY = "synthetic-provider-key-secret"
BASE = "http://127.0.0.1:8766"
GOOD = {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}


def registry():
    return {
        "p": ProviderConfig(
            "p",
            (EndpointConfig("e", "openai_chat", "https://upstream.test/v1", KEY),),
            ScanPolicy(concurrency=1, max_requests=5, retries=0),
        )
    }


@asynccontextmanager
async def agent_client(handler=None, *, providers=None, **options):
    handler = handler or (lambda r: httpx.Response(200, json=GOOD))
    app = create_app(
        registry() if providers is None else providers,
        token=TOKEN,
        http_transport=httpx.MockTransport(handler),
        **options,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app),
            base_url=BASE,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as client:
            yield client, app.state.manager


async def finished(client, scan_id):
    async with asyncio.timeout(3):
        while True:
            response = await client.get(f"/scans/{scan_id}")
            response.raise_for_status()
            data = response.json()
            if data["state"] in ("completed", "failed", "cancelled"):
                return data
            await asyncio.sleep(0.005)


async def test_agent_create_progress_and_download():
    async with agent_client() as (client, manager):
        response = await client.post(
            "/scans", json={"provider_id": "p", "models": ["m"], "max_tokens": 128}
        )
        assert response.status_code == 202
        scan_id = response.json()["scan_id"]
        data = await finished(client, scan_id)
        assert data["state"] == "completed" and data["progress"]["finished"] == 1
        response = await client.get(f"/scans/{scan_id}/report")
        report = response.json()
        assert report["results"][0]["status"] == "available"
        assert KEY not in response.text and TOKEN not in response.text
        csv_response = await client.get(f"/scans/{scan_id}/report?format=csv")
        assert csv_response.content.startswith(b"\xef\xbb\xbf")
        row = next(csv.DictReader(io.StringIO(csv_response.content.decode("utf-8-sig"))))
        assert (
            row[CSV_FIELD_LABELS["max_tokens"]] == "128"
            and row[CSV_FIELD_LABELS["status"]] == "available"
        )
        assert "no-store" in csv_response.headers["cache-control"]
        xlsx_response = await client.get(f"/scans/{scan_id}/report?format=xlsx")
        assert xlsx_response.status_code == 200
        assert xlsx_response.content[:2] == b"PK"
        assert xlsx_response.headers["content-type"].startswith(
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        )
        disposition = xlsx_response.headers["content-disposition"]
        assert re.search(r'filename="p-\d{8}-\d{6}\.xlsx"', disposition)
        passed_response = await client.get(f"/scans/{scan_id}/report?format=xlsx&scope=passed")
        assert passed_response.status_code == 200
        passed_sheet = load_workbook(io.BytesIO(passed_response.content)).active
        assert passed_sheet.auto_filter.ref == passed_sheet.dimensions == "A1:T2"
        header = [cell.value for cell in next(passed_sheet.iter_rows(max_row=1))]
        status_col = header.index("模型状态")
        statuses = [row[status_col] for row in passed_sheet.iter_rows(min_row=2, values_only=True)]
        assert statuses and all(value == "通过" for value in statuses)
        assert "仅通过" in unquote(passed_response.headers["content-disposition"])
        bad_scope = await client.get(f"/scans/{scan_id}/report?format=json&scope=passed")
        assert bad_scope.status_code == 422
        assert manager.gates["p"].sent == 1  # downloads never rescan
        inventory = await client.get("/providers")
        assert KEY not in inventory.text
        assert inventory.json()["providers"][0]["requests_remaining"] == 4


async def test_budget_preview_uses_resolved_options_without_leaking_or_scheduling():
    providers = {
        "p": ProviderConfig(
            "private-provider",
            (
                EndpointConfig(
                    KEY, "openai_chat", "https://upstream.test/v1", "another-secret-key"
                ),
                EndpointConfig("second", "anthropic_messages", "https://other.test/v1", KEY),
            ),
            ScanPolicy(max_requests=5, max_models=2, retries=2),
        )
    }
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(200, json=GOOD)

    async with agent_client(handler, providers=providers) as (client, manager):
        response = await client.post(
            "/scans/preview",
            json={
                "provider_id": "p",
                "endpoint_id": KEY,
                "mode": "quick",
                "models": ["model-id-private"],
                "max_tokens": 37,
            },
        )
        assert response.status_code == 200
        preview = response.json()
        assert preview["endpoints"] == [
            {
                "id": "***REDACTED***",
                "protocol": "openai_chat",
                "max_tokens": 37,
                "target_count": 1,
                "source": "manual",
            }
        ]
        assert preview["request_upper_bound"] == preview["effective_max_requests"] == 1
        assert preview["requests_remaining"] == 5 and preview["retries"] == 0
        for private in (
            KEY,
            "another-secret-key",
            "model-id-private",
            "https://",
            "private-provider",
        ):
            assert private not in response.text
        assert not calls and not manager.jobs and manager.gates["p"].sent == 0


async def test_xlsx_passed_scope_with_no_passed_models():
    def missing(request):
        return httpx.Response(404, json={"error": {"code": "model_not_found"}})

    async with agent_client(missing) as (client, manager):
        response = await client.post("/scans", json={"provider_id": "p", "models": ["missing"]})
        assert response.status_code == 202
        scan_id = response.json()["scan_id"]
        assert (await finished(client, scan_id))["report_available"]
        report_url = f"/scans/{scan_id}/report?format=xlsx"
        all_response = await client.get(report_url)
        passed_response = await client.get(f"{report_url}&scope=passed")
        assert all_response.status_code == passed_response.status_code == 200
        all_sheet = load_workbook(io.BytesIO(all_response.content)).active
        passed_sheet = load_workbook(io.BytesIO(passed_response.content)).active
        assert all_sheet.auto_filter.ref == all_sheet.dimensions == "A1:T2"
        assert passed_sheet.auto_filter.ref == passed_sheet.dimensions == "A1:T1"
        assert list(passed_sheet.values) == [next(all_sheet.values)]
        assert manager.gates["p"].sent == 1


async def test_history_create_failure_prevents_scan(tmp_path, monkeypatch):
    history = HistoryStore(tmp_path / "scans.sqlite3")

    def unavailable(view, conditions):
        raise HistoryError("simulated disk error")

    monkeypatch.setattr(history, "create", unavailable)
    async with agent_client(history_store=history) as (client, manager):
        response = await client.post("/scans", json={"provider_id": "p", "models": ["good"]})
        assert response.status_code == 503
        assert response.json()["detail"] == "Scan history could not be saved"
        assert not manager.jobs and manager.gates["p"].sent == 0


@pytest.mark.parametrize("fail_at", ["checkpoint", "finish"])
async def test_history_write_failure_preserves_last_durable_results(tmp_path, monkeypatch, fail_at):
    history = HistoryStore(tmp_path / "scans.sqlite3")
    checkpoint = history.checkpoint
    finalize = history.finish

    def save_checkpoint(view, *, discovery=None, result=None):
        if fail_at == "checkpoint" and result is not None and result["model"] == "b":
            raise HistoryError("simulated disk error")
        checkpoint(view, discovery=discovery, result=result)

    def save_terminal(view, report):
        if fail_at == "finish":
            raise HistoryError("simulated disk error")
        finalize(view, report)

    monkeypatch.setattr(history, "checkpoint", save_checkpoint)
    monkeypatch.setattr(history, "finish", save_terminal)
    async with agent_client(history_store=history) as (client, _):
        created = await client.post("/scans", json={"provider_id": "p", "models": ["a", "b"]})
        assert created.status_code == 202
        scan_id = created.json()["scan_id"]
        job = await finished(client, scan_id)
        assert job["state"] == "failed"
        assert job["error"] == "Scan history could not be saved"
        if fail_at == "checkpoint":
            assert job["report_available"] and job["report_partial"]
            partial = await client.get(f"/scans/{scan_id}/report")
            assert partial.status_code == 200
            assert partial.json()["scan"]["partial"] is True
            assert partial.json()["scan"]["requests_sent"] is None
            assert [row["model"] for row in partial.json()["results"]] == ["a"]

    reopened = HistoryStore(history.path)
    async with agent_client(history_store=reopened):
        saved = reopened.get(scan_id)
        assert saved["report"] is None
        if fail_at == "checkpoint":
            assert saved["view"]["state"] == "failed"
            assert saved["view"]["progress"]["finished"] == 1
            assert [row["model"] for row in saved["results"]] == ["a"]
        else:
            assert saved["view"]["state"] == "interrupted"
            assert saved["view"]["progress"]["finished"] == 2
            assert [row["model"] for row in saved["results"]] == ["a", "b"]
        assert len(saved["discovery"]) == 1


async def test_history_summary_keeps_state_and_report_flags_in_one_snapshot(tmp_path, monkeypatch):
    history = HistoryStore(tmp_path / "scans.sqlite3")
    checkpoint = history.checkpoint
    get_view = history.get_view
    model_entered = asyncio.Event()
    finish_model = asyncio.Event()
    snapshot_read = Event()
    return_snapshot = Event()

    async def respond(request):
        if json.loads(request.content)["model"] == "b":
            model_entered.set()
            await finish_model.wait()
        return httpx.Response(200, json=GOOD)

    def fail_second_checkpoint(view, *, discovery=None, result=None):
        if result is not None and result["model"] == "b":
            raise HistoryError("simulated disk error")
        checkpoint(view, discovery=discovery, result=result)

    def read_snapshot(scan_id):
        view = get_view(scan_id)
        if not snapshot_read.is_set():
            snapshot_read.set()
            assert return_snapshot.wait(timeout=5)
        return view

    monkeypatch.setattr(history, "checkpoint", fail_second_checkpoint)
    monkeypatch.setattr(history, "get_view", read_snapshot)
    async with agent_client(respond, history_store=history) as (client, manager):
        created = await client.post("/scans", json={"provider_id": "p", "models": ["a", "b"]})
        assert created.status_code == 202
        scan_id = created.json()["scan_id"]
        await asyncio.wait_for(model_entered.wait(), timeout=5)
        pending = asyncio.create_task(client.get(f"/scans/{scan_id}"))
        try:
            assert await asyncio.to_thread(snapshot_read.wait, 5)
            finish_model.set()
            await asyncio.wait_for(asyncio.shield(manager.jobs[scan_id].task), timeout=5)
        finally:
            finish_model.set()
            return_snapshot.set()
        old = (await pending).json()
        assert old["state"] == "running" and not old["report_available"]
        current = (await client.get(f"/scans/{scan_id}")).json()
        assert current["state"] == "failed"
        assert current["report_available"] and current["report_partial"]
        report = await client.get(f"/scans/{scan_id}/report")
        assert report.status_code == 200
        assert [row["model"] for row in report.json()["results"]] == ["a"]


async def test_history_scrubs_credentials_from_conditions_and_results(tmp_path):
    history = HistoryStore(tmp_path / "scans.sqlite3")
    other_key = "secondary-secret-key"
    proxy = "http://u9:p7@127.0.0.1:1"
    providers = {
        "p": ProviderConfig(
            "p",
            (
                EndpointConfig("e", "openai_chat", "https://upstream.test/v1", KEY),
                EndpointConfig("x", "openai_chat", "https://other.test/v1", other_key, proxy=proxy),
            ),
            ScanPolicy(retries=0),
        )
    }

    def handler(request):
        return httpx.Response(
            403, json={"error": {"message": f"{KEY} {other_key} user=u9 pass=p7"}}
        )

    async with agent_client(handler, providers=providers, history_store=history) as (client, _):
        created = await client.post(
            "/scans", json={"provider_id": "p", "endpoint_id": "e", "models": [other_key]}
        )
        assert created.status_code == 202
        scan_id = created.json()["scan_id"]
        assert (await finished(client, scan_id))["state"] == "completed"

    saved = history.get(scan_id)
    assert saved["conditions"]["provider_revision"] is None
    assert saved["conditions"]["endpoints"][0]["models"] == ["***REDACTED***"]
    assert saved["results"][0]["error_category"] == "FORBIDDEN"
    assert "***REDACTED***" in saved["results"][0]["error_message"]
    serialized = json.dumps(saved)
    for secret in (KEY, other_key, TOKEN, "user=u9", "pass=p7", proxy):
        assert secret not in serialized
        assert secret.encode() not in history.path.read_bytes()


@pytest.mark.parametrize(
    "scenario,state,expected",
    [
        ("complete", "completed", [3, 3, 3, 0, 0, 3]),
        ("cancel", "cancelled", [3, 3, 1, 1, 1, 3]),
        ("budget", "failed", [3, 3, 1, 2, 0, 3]),
        ("model_limit", "failed", [5, 3, 3, 0, 0, 5]),
    ],
)
async def test_two_endpoint_scan_progress(scenario, state, expected):
    providers = {
        "p": ProviderConfig(
            "p",
            (
                EndpointConfig(
                    "openai", "openai_chat", "https://upstream.test/v1", KEY, models=("a", "b")
                ),
                EndpointConfig(
                    "anthropic",
                    "anthropic_messages",
                    "https://upstream.test/v1",
                    KEY,
                    models=("c", "d", "e") if scenario == "model_limit" else ("c",),
                ),
            ),
            ScanPolicy(concurrency=1, max_models=3, max_requests=5, retries=0),
        )
    }
    entered_second = asyncio.Event()

    async def handler(request):
        if scenario == "cancel" and json.loads(request.content)["model"] == "b":
            entered_second.set()
            await asyncio.Event().wait()
        if request.url.path.endswith("/messages"):
            return httpx.Response(403, json={"error": {"message": "Denied"}})
        return httpx.Response(200, json=GOOD)

    async with agent_client(handler, providers=providers) as (client, _):
        payload = {"provider_id": "p", "endpoint_id": "all"}
        if scenario == "budget":
            payload["max_requests"] = 1
        created = await client.post("/scans", json=payload)
        assert created.status_code == 202
        scan_id = created.json()["scan_id"]
        if scenario == "cancel":
            await asyncio.wait_for(entered_second.wait(), 2)
            running = (await client.get(f"/scans/{scan_id}")).json()
            assert running["state"] == "running"
            assert running["progress"] == {
                "discovered": 3,
                "planned": 3,
                "finished": 1,
                "skipped": 0,
                "cancelled": 0,
            }
            await client.post(f"/scans/{scan_id}/cancel")
        done = await finished(client, scan_id)
        assert done["state"] == state
        keys = ("discovered", "planned", "finished", "skipped", "cancelled", "total")
        assert done["progress"] == dict(zip(keys, expected, strict=True))


@pytest.mark.parametrize(
    "operation,models,state", [("models", ["a"], "completed"), ("scan", [], "failed")]
)
async def test_progress_without_verification_targets(operation, models, state):
    def catalog(request):
        assert request.method == "GET"
        return httpx.Response(200, json={"data": [{"id": model} for model in models]})

    async with agent_client(catalog) as (client, _):
        created = await client.post("/scans", json={"provider_id": "p", "operation": operation})
        assert created.status_code == 202
        done = await finished(client, created.json()["scan_id"])
        assert done["state"] == state
        expected = {"discovered": len(models), "finished": 0, "total": 0}
        if operation == "scan":
            expected.update(planned=0, skipped=0, cancelled=0)
        assert done["progress"] == expected


async def test_cancel_during_discovery_keeps_candidates_without_plan():
    providers = {
        "p": ProviderConfig(
            "p",
            (
                EndpointConfig("openai", "openai_chat", "https://upstream.test/openai/v1", KEY),
                EndpointConfig(
                    "anthropic", "anthropic_messages", "https://upstream.test/anthropic/v1", KEY
                ),
            ),
            ScanPolicy(retries=0),
        )
    }
    second_discovery = asyncio.Event()

    async def handler(request):
        assert request.method == "GET"
        if "/anthropic/" in request.url.path:
            second_discovery.set()
            await asyncio.Event().wait()
        return httpx.Response(200, json={"data": [{"id": "a"}, {"id": "b"}]})

    async with agent_client(handler, providers=providers) as (client, _):
        created = await client.post("/scans", json={"provider_id": "p", "endpoint_id": "all"})
        assert created.status_code == 202
        scan_id = created.json()["scan_id"]
        await asyncio.wait_for(second_discovery.wait(), 2)
        await client.post(f"/scans/{scan_id}/cancel")
        done = await finished(client, scan_id)
        assert done["state"] == "cancelled"
        assert done["progress"] == {"discovered": 2, "finished": 0, "total": 2}


@pytest.mark.parametrize(
    "headers,status",
    [
        ({"Authorization": "Bearer wrong"}, 401),
        ({"Authorization": ""}, 401),
        ({"Host": "evil.test:8766"}, 403),
        ({"Origin": "https://evil.test"}, 403),
        ({"Origin": "null"}, 403),
        ({"Host": "localhost:9999"}, 403),
        ({"Origin": "http://127.0.0.1:8766/path"}, 403),
    ],
)
async def test_boundary_denials(headers, status):
    async with agent_client() as (client, manager):
        response = await client.post(
            "/scans", headers=headers, json={"provider_id": "p", "models": ["m"]}
        )
        assert response.status_code == status
        assert not manager.jobs


async def test_duplicate_security_headers_and_same_origin():
    async with agent_client() as (client, manager):
        response = await client.get(
            "/providers",
            headers=[("Authorization", f"Bearer {TOKEN}"), ("Authorization", f"Bearer {TOKEN}")],
        )
        assert response.status_code == 401
        response = await client.get(
            "/providers", headers=[("Host", "127.0.0.1:8766"), ("Host", "evil.test")]
        )
        assert response.status_code == 403
        response = await client.get("/providers", headers={"Origin": BASE})
        assert response.status_code == 200
        response = await client.get("/health", headers={"Authorization": ""})
        assert response.status_code == 200 and set(response.json()) == {"status", "version"}
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/scans/missing")).status_code == 404


@pytest.mark.parametrize(
    "extra",
    [
        {"api_key": "private-input"},
        {"base_url": "https://evil.test"},
        {"out": "private-input"},
        {"max_tokens": 0},
        {"max_requests": 6},
        {"scan_timeout": 301},
        {"models": []},
        {"max_tokens": True},
        {"models": "m"},
        {"endpoint_id": "absent"},
    ],
)
async def test_bad_input_does_not_echo_or_scan(extra):
    async with agent_client() as (client, manager):
        response = await client.post("/scans", json={"provider_id": "p", "models": ["m"], **extra})
        assert response.status_code == 422
        assert "private-input" not in response.text
        assert manager.gates["p"].sent == 0
        assert not manager.jobs


async def test_oversize_and_wrong_content_type():
    async with agent_client() as (client, manager):
        response = await client.post(
            "/scans", content=b"x" * 65537, headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        response = await client.post("/scans", content="provider_id=p")
        assert response.status_code == 415
        response = await client.post(
            "/scans", content="not JSON", headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 422
        assert not manager.jobs


async def test_queue_capacity_cancel_and_shutdown():
    entered = asyncio.Event()
    release = asyncio.Event()

    async def slow(request):
        entered.set()
        await release.wait()
        return httpx.Response(200, json=GOOD)

    async with agent_client(slow, max_running=1, max_queued=1) as (client, manager):
        a = (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()[
            "scan_id"
        ]
        await entered.wait()
        b = (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()[
            "scan_id"
        ]
        assert (
            await client.post("/scans", json={"provider_id": "p", "models": ["m"]})
        ).status_code == 429
        assert (await client.get(f"/scans/{a}/report")).status_code == 409
        assert (await client.post(f"/scans/{b}/cancel")).json()["state"] == "cancelled"
        assert (await client.post(f"/scans/{b}/cancel")).json()["state"] == "cancelled"
        assert manager.gates["p"].sent == 1
        await client.post(f"/scans/{a}/cancel")
        assert (await finished(client, a))["state"] == "cancelled"
        report = (await client.get(f"/scans/{a}/report")).json()
        assert report["results"][0]["status"] == "cancelled"
    assert not manager.tasks


async def test_shutdown_cancels_running_and_queued():
    entered = asyncio.Event()

    async def slow(request):
        entered.set()
        await asyncio.Event().wait()

    async with agent_client(slow, max_running=1) as (client, manager):
        a = (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()[
            "scan_id"
        ]
        await entered.wait()
        b = (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()[
            "scan_id"
        ]
    assert manager.jobs[a].state == manager.jobs[b].state == "cancelled"
    assert not manager.tasks


async def test_history_eviction_and_provider_budget():
    async with agent_client(max_history=1) as (client, manager):
        previous = None
        for _ in range(6):
            response = await client.post("/scans", json={"provider_id": "p", "models": ["m"]})
            current = response.json()["scan_id"]
            result = await finished(client, current)
            if previous:
                assert (await client.get(f"/scans/{previous}")).status_code == 404
            previous = current
        assert manager.gates["p"].sent == 5
        assert result["state"] == "failed"
        report = (await client.get(f"/scans/{current}/report")).json()
        assert report["results"][0]["status"] == "skipped"


async def test_failure_messages_do_not_leak_provider_key():
    def handler(request):
        return httpx.Response(403, json={"error": {"message": KEY}})

    async with agent_client(handler) as (client, manager):
        scan_id = (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()[
            "scan_id"
        ]
        await finished(client, scan_id)
        report = await client.get(f"/scans/{scan_id}/report")
        assert KEY not in report.text
        assert report.json()["results"][0]["error_category"] == "FORBIDDEN"


async def test_internal_exception_is_not_echoed():
    def handler(request):
        raise RuntimeError(KEY)

    async with agent_client(handler) as (client, manager):
        scan_id = (await client.post("/scans", json={"provider_id": "p", "models": ["m"]})).json()[
            "scan_id"
        ]
        state = await finished(client, scan_id)
        assert state["state"] == "failed"
        assert KEY not in str(state)
        assert (await client.get(f"/scans/{scan_id}/report")).status_code == 409


async def test_chunked_body_limit_without_content_length():
    async def chunks():
        for _ in range(5):
            yield b"x" * 16384

    async with agent_client() as (client, manager):
        response = await client.post(
            "/scans", content=chunks(), headers={"Content-Type": "application/json"}
        )
        assert response.status_code == 413
        assert not manager.jobs
