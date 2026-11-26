import csv
import importlib.util
import io
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import pytest
from openpyxl import load_workbook

from examples.mock_server import MockHandler, running_mock
from llm_model_scout.agent.history import HistoryStore

ROOT = Path(__file__).resolve().parents[2]
KEY = "synthetic-process-key"
spec = importlib.util.spec_from_file_location("agent_demo", ROOT / "examples/agent_demo.py")
demo = importlib.util.module_from_spec(spec)
spec.loader.exec_module(demo)


def wait_done(client, scan_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        response = client.get(f"/scans/{scan_id}")
        response.raise_for_status()
        result = response.json()
        if result["state"] in ("completed", "failed", "cancelled"):
            return result
        time.sleep(0.01)
    raise AssertionError("Agent task did not finish")


def create_scan(client, provider_id="p", **options):
    response = client.post("/scans", json={"provider_id": provider_id, **options})
    assert response.status_code == 202
    return response.json()["scan_id"]


@pytest.fixture
def local_agent(http_server, tmp_path):
    base, upstream = http_server
    config = tmp_path / "registry.toml"
    config.write_text(
        f'''[[providers]]
name = "p"
retries = 0
concurrency = 1
[[providers.endpoints]]
id = "e"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
[[providers]]
name = "q"
retries = 0
concurrency = 1
[[providers.endpoints]]
id = "e"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
''',
        encoding="utf-8",
    )
    env = {**os.environ, "SCOUT_PROCESS_TEST_KEY": KEY}
    data_dir = tmp_path / "data"

    def run_agent(*extra_args):
        return demo.running_agent_process(config, env, ["--data-dir", str(data_dir), *extra_args])

    return run_agent, HistoryStore(data_dir / "scans.sqlite3"), upstream


def test_real_agent_process_lifecycle(local_agent):
    run_agent, _, upstream = local_agent
    with run_agent() as (client, _):
        assert client.get("/providers", headers={"Authorization": ""}).status_code == 401
        created = client.post("/scans", json={"provider_id": "p", "models": ["good"]})
        assert created.status_code == 202
        scan_id = created.json()["scan_id"]
        assert wait_done(client, scan_id)["state"] == "completed"
        report = client.get(f"/scans/{scan_id}/report")
        assert report.json()["results"][0]["status"] == "available"
        assert KEY not in report.text
        assert client.get(f"/scans/{scan_id}/report?format=csv").content.startswith(b"\xef\xbb\xbf")
        slow_id = client.post("/scans", json={"provider_id": "p", "models": ["slow"]}).json()[
            "scan_id"
        ]
        deadline = time.monotonic() + 5
        while not any(body and body.get("model") == "slow" for _, _, _, body in upstream):
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert client.post(f"/scans/{slow_id}/cancel").status_code == 200
        assert wait_done(client, slow_id)["state"] == "cancelled"
        partial_report = client.get(f"/scans/{slow_id}/report").json()
        assert partial_report["results"][0]["status"] == "cancelled"


def test_completed_history_survives_agent_restart(local_agent):
    run_agent, history, upstream = local_agent
    with run_agent() as (client, _):
        scan_id = create_scan(client, models=["good", "bad"])
        assert wait_done(client, scan_id)["state"] == "completed"
        report = client.get(f"/scans/{scan_id}/report")
        assert report.status_code == 200
        assert KEY not in report.text
        discovery_id = create_scan(client, "q", operation="models")
        assert wait_done(client, discovery_id)["state"] == "completed"

    prior_requests = list(upstream)
    assert len(prior_requests) == 3
    with run_agent() as (client, _):
        saved = history.get(scan_id)
        assert saved["view"]["state"] == "completed"
        assert saved["report"] == report.json()
        assert saved["results"] == saved["report"]["results"]
        assert saved["discovery"] == saved["report"]["discovery"]
        assert KEY not in json.dumps(saved)

        detail_url = f"/scans/{scan_id}"
        report_url = f"{detail_url}/report"
        for url in ("/scans", detail_url, report_url):
            assert client.get(url, headers={"Authorization": ""}).status_code == 401

        first = client.get("/scans", params={"limit": 1, "offset": 0})
        first.raise_for_status()
        page = first.json()
        assert (page["total"], page["limit"], page["offset"]) == (2, 1, 0)
        assert [scan["scan_id"] for scan in page["scans"]] == [discovery_id]
        assert {item["provider_id"] for item in page["providers"]} == {"p", "q"}

        second = client.get("/scans", params={"limit": 1, "offset": 1})
        second.raise_for_status()
        assert [scan["scan_id"] for scan in second.json()["scans"]] == [scan_id]
        filtered = client.get("/scans", params={"provider_id": "p", "limit": 1})
        filtered.raise_for_status()
        assert filtered.json()["total"] == 1
        assert [scan["scan_id"] for scan in filtered.json()["scans"]] == [scan_id]
        assert {item["provider_id"] for item in filtered.json()["providers"]} == {"p", "q"}

        restored = client.get(detail_url)
        restored.raise_for_status()
        assert restored.json()["state"] == "completed"
        assert restored.json()["report_available"] is True
        assert client.post(f"{detail_url}/cancel").json() == restored.json()
        restored_report = client.get(report_url)
        restored_report.raise_for_status()
        assert restored_report.json() == report.json()
        assert "attachment" in restored_report.headers["content-disposition"]
        assert client.get(f"/scans/{discovery_id}/report?format=xlsx").status_code == 422
    assert upstream == prior_requests


def test_agent_recovers_partial_history_after_process_kill(local_agent):
    run_agent, history, upstream = local_agent
    with run_agent("--max-running", "1") as (client, process):
        scan_id = create_scan(client, models=["good", "slow"])
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            pending = history.get(scan_id)
            slow_started = any(body and body["model"] == "slow" for _, _, _, body in upstream)
            if pending is not None and pending["results"] and slow_started:
                break
            time.sleep(0.01)
        else:
            pytest.fail("Agent did not commit a result while the next model was pending")
        assert pending["view"]["state"] == "running"
        assert pending["view"]["progress"]["finished"] == 1
        assert pending["view"]["progress"]["planned"] == 2
        assert pending["report"] is None
        assert [(row["endpoint_id"], row["models"]) for row in pending["discovery"]] == [
            ("e", ["good", "slow"])
        ]
        assert [(row["model"], row["status"]) for row in pending["results"]] == [
            ("good", "available")
        ]
        queued_id = create_scan(client, models=["bad"])
        assert client.get(f"/scans/{queued_id}").json()["state"] == "queued"
        process.kill()
        process.wait(timeout=10)

    prior_requests = list(upstream)
    assert len(prior_requests) == 2
    with run_agent() as (client, _):
        recovered = history.get(scan_id)
        assert recovered["view"]["state"] == "interrupted"
        assert recovered["view"]["finished_at"]
        assert recovered["view"]["progress"]["finished"] == 1
        assert recovered["report"] is None
        assert recovered["conditions"] == pending["conditions"]
        assert recovered["discovery"] == pending["discovery"]
        assert recovered["results"] == pending["results"]
        assert KEY not in json.dumps(recovered)

        detail = client.get(f"/scans/{scan_id}")
        detail.raise_for_status()
        assert detail.json()["state"] == "interrupted"
        assert detail.json()["report_available"] is True
        assert detail.json()["report_partial"] is True

        report_url = f"/scans/{scan_id}/report"
        response = client.get(report_url)
        response.raise_for_status()
        report = response.json()
        assert report["scan"]["status"] == "interrupted"
        assert report["scan"]["partial"] is True
        assert report["scan"]["requests_sent"] is None
        assert report["results"] == pending["results"]
        assert report["discovery"] == pending["discovery"]
        assert report["summary"]["total"] == 1
        assert KEY not in response.text

        csv_report = client.get(report_url, params={"format": "csv"})
        csv_report.raise_for_status()
        assert csv_report.content.startswith(b"\xef\xbb\xbf")
        rows = list(csv.DictReader(io.StringIO(csv_report.content.decode("utf-8-sig"))))
        assert [(row["模型"], row["模型状态"]) for row in rows] == [("good", "available")]

        xlsx_report = client.get(report_url, params={"format": "xlsx", "scope": "passed"})
        xlsx_report.raise_for_status()
        sheet = load_workbook(io.BytesIO(xlsx_report.content)).active
        rows = list(sheet.values)
        assert [row[rows[0].index("模型")] for row in rows[1:]] == ["good"]

        queued = client.get(f"/scans/{queued_id}")
        queued.raise_for_status()
        assert queued.json()["state"] == "interrupted"
        assert queued.json()["report_available"] is False
        assert client.get(f"/scans/{queued_id}/report").status_code == 409
        assert upstream == prior_requests

        new_id = create_scan(client, models=["bad"])
        assert wait_done(client, new_id)["state"] == "completed"
    assert upstream[:-1] == prior_requests
    assert upstream[-1][-1]["model"] == "bad"


def test_disk_retention_prunes_old_terminal_scan_and_keeps_active_scan(local_agent):
    run_agent, history, upstream = local_agent
    with run_agent("--max-history", "1") as (client, _):
        oldest_id = create_scan(client, models=["good"])
        assert wait_done(client, oldest_id)["state"] == "completed"

    with run_agent("--max-history", "1") as (client, _):
        active_id = create_scan(client, models=["slow"])
        deadline = time.monotonic() + 5
        while not any(body and body["model"] == "slow" for _, _, _, body in upstream):
            assert time.monotonic() < deadline
            time.sleep(0.01)

        newest_id = create_scan(client, "q", models=["good"])
        assert wait_done(client, newest_id)["state"] == "completed"
        assert history.get(oldest_id) is None
        assert client.get(f"/scans/{oldest_id}").status_code == 404
        assert history.get(active_id)["view"]["state"] == "running"
        page = client.get("/scans")
        page.raise_for_status()
        assert page.json()["total"] == 2
        assert [scan["scan_id"] for scan in page.json()["scans"]] == [newest_id, active_id]
        assert client.post(f"/scans/{active_id}/cancel").status_code == 200
        assert wait_done(client, active_id)["state"] == "cancelled"


@pytest.fixture
def comparison_agent(tmp_path):
    modes = {
        "baseline": ("regress", "recover", "stable", "removed"),
        "current": ("regress", "recover", "stable", "added"),
        "partial": ("regress",),
    }
    phase = "baseline"
    requests = []
    release = threading.Event()
    waiting = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, document):
            body = json.dumps(document).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            requests.append(("GET", self.path, None))
            uri = urlsplit(self.path)
            if uri.path.startswith("/anthropic/"):
                self.respond(200, {"data": [{"id": "regress"}]})
            elif uri.query:
                self.respond(503, {"error": {"message": "Synthetic pagination failure"}})
            else:
                data = {"data": [{"id": model} for model in modes[phase]]}
                if phase == "partial":
                    data.update(has_more=True, last_id="regress")
                self.respond(200, data)

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, payload))
            model = payload["model"]
            if model == "hold":
                waiting.set()
                release.wait()
            if self.path.startswith("/anthropic/"):
                self.respond(
                    200,
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "text", "text": "pong"}],
                    },
                )
            elif model == ("recover" if phase == "baseline" else "regress"):
                self.respond(403, {"error": {"message": "Synthetic model denied"}})
            else:
                self.respond(
                    200, {"choices": [{"message": {"role": "assistant", "content": "pong"}}]}
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        config = tmp_path / "comparison.toml"
        env = {**os.environ, "SCOUT_PROCESS_TEST_KEY": KEY}

        def run_agent(stage, *, openai_path="/openai/v1", max_requests=1000, max_history=50):
            nonlocal phase
            phase = stage
            openai_endpoint = (
                f'''[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "{base}{openai_path}"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
'''
                if openai_path is not None
                else ""
            )
            config.write_text(
                f'''[[providers]]
name = "p"
retries = 0
concurrency = 1
max_requests = {max_requests}
{openai_endpoint}[[providers.endpoints]]
id = "anthropic"
protocol = "anthropic_messages"
base_url = "{base}/anthropic/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
[[providers]]
name = "q"
retries = 0
concurrency = 1
[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "{base}/openai/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
''',
                encoding="utf-8",
            )
            return demo.running_agent_process(
                config,
                env,
                ["--data-dir", str(tmp_path / "data"), "--max-history", str(max_history)],
            )

        yield run_agent, requests, waiting
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def compare(client, baseline_id, current_id, endpoint_id=None, *, headers=None):
    params = {"baseline_id": baseline_id, "current_id": current_id}
    if endpoint_id is not None:
        params["endpoint_id"] = endpoint_id
    return client.get("/scans/compare", params=params, headers=headers)


def test_compare_snapshots_across_agent_sessions(comparison_agent):
    run_agent, upstream, _ = comparison_agent
    with run_agent("baseline") as (client, _):
        baseline_id = create_scan(client, endpoint_id="all")
        assert wait_done(client, baseline_id)["state"] == "completed"

    with run_agent("current") as (client, _):
        current_id = create_scan(client, endpoint_id="all")
        assert wait_done(client, current_id)["state"] == "completed"
        before = list(upstream)

        unauthenticated = compare(client, baseline_id, current_id, headers={"Authorization": ""})
        assert unauthenticated.status_code == 401

        options = compare(client, baseline_id, current_id)
        options.raise_for_status()
        selection = options.json()
        assert selection["baseline"]["scan_id"] == baseline_id
        assert selection["current"]["scan_id"] == current_id
        assert {(row["id"], row["protocol"]) for row in selection["endpoints"]} == {
            ("openai", "openai_chat"),
            ("anthropic", "anthropic_messages"),
        }
        assert selection["endpoint_id"] is None and selection["rows"] == []

        openai = compare(client, baseline_id, current_id, "openai")
        openai.raise_for_status()
        assert KEY not in openai.text
        result = openai.json()
        assert result["conditions"]["credentials"] == "unknown"
        assert result["catalog"] == {
            "baseline": "complete",
            "current": "complete",
            "comparable": True,
        }
        rows = {row["model"]: row for row in result["rows"]}
        assert rows["regress"]["verification"] == "regressed"
        assert (rows["regress"]["baseline_status"], rows["regress"]["current_status"]) == (
            "available",
            "unavailable",
        )
        assert rows["recover"]["verification"] == "recovered"
        assert rows["stable"]["verification"] == "unchanged"
        assert rows["removed"]["verification"] == "not_tested"
        assert rows["removed"]["catalog"] == "removed"
        assert rows["added"]["verification"] == "not_tested"
        assert rows["added"]["catalog"] == "added"

        anthropic = compare(client, baseline_id, current_id, "anthropic")
        anthropic.raise_for_status()
        (row,) = anthropic.json()["rows"]
        assert row["model"] == "regress" and row["verification"] == "unchanged"
        assert row["baseline_status"] == row["current_status"] == "available"
        assert upstream == before

        manual_id = create_scan(client, endpoint_id="openai", models=["regress", "stable"])
        assert wait_done(client, manual_id)["state"] == "completed"
        limited_id = create_scan(client, endpoint_id="openai", max_tokens=64)
        assert wait_done(client, limited_id)["state"] == "completed"
        before = list(upstream)

        manual = compare(client, baseline_id, manual_id)
        manual.raise_for_status()
        result = manual.json()
        assert result["endpoint_id"] == "openai"
        assert result["catalog"]["current"] == "manual"
        assert result["catalog"]["comparable"] is False
        rows = {row["model"]: row for row in result["rows"]}
        assert rows["regress"]["verification"] == "regressed"
        assert rows["removed"]["verification"] == "not_tested"
        assert rows["removed"]["catalog"] is None
        assert compare(client, baseline_id, manual_id, "anthropic").status_code == 422

        changed = compare(client, baseline_id, limited_id)
        changed.raise_for_status()
        result = changed.json()
        assert result["conditions"]["status"] == "changed"
        assert any(
            change["field"] == "probe.max_tokens" for change in result["conditions"]["changes"]
        )
        rows = {row["model"]: row for row in result["rows"]}
        assert rows["regress"]["current_status"] == "unavailable"
        assert rows["regress"]["verification"] == "conditions_changed"
        assert upstream == before

    with run_agent("partial") as (client, _):
        partial_id = create_scan(client, endpoint_id="openai")
        assert wait_done(client, partial_id)["state"] == "failed"
        before = list(upstream)
        partial = compare(client, baseline_id, partial_id)
        partial.raise_for_status()
        result = partial.json()
        assert result["catalog"] == {
            "baseline": "complete",
            "current": "partial",
            "comparable": False,
        }
        rows = {row["model"]: row for row in result["rows"]}
        assert rows["removed"]["current_status"] is None
        assert rows["removed"]["verification"] == "not_tested"
        assert rows["removed"]["catalog"] is None
        assert upstream == before


def test_compare_rejects_incompatible_or_active_runs(comparison_agent):
    run_agent, upstream, waiting = comparison_agent
    with run_agent("current") as (client, _):
        baseline = create_scan(client, endpoint_id="openai", models=["stable"])
        other_endpoint = create_scan(client, endpoint_id="anthropic", models=["stable"])
        other_provider = create_scan(client, "q", models=["stable"])
        for scan_id in (baseline, other_endpoint, other_provider):
            assert wait_done(client, scan_id)["state"] == "completed"
        active = create_scan(client, endpoint_id="openai", models=["hold", "stable"])
        assert waiting.wait(5)
        try:
            before = list(upstream)
            assert compare(client, baseline, baseline).status_code == 422
            assert compare(client, baseline, other_endpoint).status_code == 422
            assert compare(client, baseline, other_provider).status_code == 422
            assert compare(client, baseline, "0" * 32).status_code == 404
            assert compare(client, baseline, active).status_code == 409
            assert upstream == before
        finally:
            assert client.post(f"/scans/{active}/cancel").status_code == 200
            assert wait_done(client, active)["state"] == "cancelled"

        cancelled = compare(client, baseline, active)
        cancelled.raise_for_status()
        rows = {row["model"]: row for row in cancelled.json()["rows"]}
        assert rows["hold"]["current_status"] == "cancelled"
        assert rows["hold"]["verification"] == "not_tested"
        assert rows["stable"]["current_status"] == "skipped"
        assert rows["stable"]["verification"] == "not_tested"
        assert upstream == before


def retest_payload(baseline_id, current_id):
    return {
        "baseline_id": baseline_id,
        "current_id": current_id,
        "endpoint_id": "openai",
        "models": ["regress", "recover"],
        "request_id": str(uuid4()),
    }


def test_retest_runs_selected_models_once_and_reuses_saved_probe(comparison_agent):
    run_agent, upstream, _ = comparison_agent
    options = {"endpoint_id": "all", "profile": "chat-basic", "max_tokens": 77}
    with run_agent("baseline") as (client, _):
        baseline_id = create_scan(client, **options)
        assert wait_done(client, baseline_id)["state"] == "completed"

    with run_agent("current") as (client, _):
        current_id = create_scan(client, **options)
        assert wait_done(client, current_id)["state"] == "completed"
        payload = retest_payload(baseline_id, current_id)
        source = {key: payload[key] for key in ("baseline_id", "current_id", "endpoint_id")}
        before = len(upstream)
        assert (
            client.post("/scans/retest", json=payload, headers={"Authorization": ""}).status_code
            == 401
        )
        missing = client.post("/scans/retest", json={**payload, "current_id": "0" * 32})
        assert missing.status_code == 404
        for model in ("stable", "not-in-diff"):
            assert (
                client.post("/scans/retest", json={**payload, "models": [model]}).status_code == 422
            )
        assert len(upstream) == before

        def submit():
            return client.post("/scans/retest", json=payload)

        with ThreadPoolExecutor(max_workers=2) as workers:
            one = workers.submit(submit)
            two = workers.submit(submit)
            responses = (one.result(timeout=5), two.result(timeout=5))
        assert [response.status_code for response in responses] == [202, 202]
        jobs = [response.json() for response in responses]
        assert jobs[0]["scan_id"] == jobs[1]["scan_id"]
        assert all(job["source"] == source for job in jobs)
        retest_id = jobs[0]["scan_id"]
        finished = wait_done(client, retest_id)
        assert finished["state"] == "completed" and finished["source"] == source
        assert finished["progress"]["finished"] == 2
        report = client.get(f"/scans/{retest_id}/report").json()
        assert report["scan"]["source"] == source
        assert report["discovery"][0]["source"] == "manual"
        assert [r["model"] for r in report["results"]] == ["recover", "regress"]
        assert [(method, path) for method, path, _ in upstream[before:]] == [
            ("POST", "/openai/v1/chat/completions"),
            ("POST", "/openai/v1/chat/completions"),
        ]
        assert [body for _, _, body in upstream[before:]] == [
            {
                "model": model,
                "max_tokens": 77,
                "messages": [{"role": "user", "content": "ping"}],
                "stream": False,
            }
            for model in ("recover", "regress")
        ]
        assert (
            client.post("/scans/retest", json={**payload, "models": ["recover"]}).status_code == 409
        )

    before = list(upstream)
    with run_agent("current") as (client, _):
        duplicate = client.post("/scans/retest", json=payload)
        assert duplicate.status_code == 202 and duplicate.json()["scan_id"] == retest_id
        restored = client.get(f"/scans/{retest_id}")
        assert restored.status_code == 200 and restored.json()["source"] == source
        page = client.get("/scans", params={"provider_id": "p"}).json()
        assert page["total"] == 3 and page["scans"][0]["source"] == source

    with run_agent("current", max_history=1) as (client, _):
        assert client.get(f"/scans/{baseline_id}").status_code == 404
        assert client.get(f"/scans/{current_id}").status_code == 404
        duplicate = client.post("/scans/retest", json=payload)
        assert duplicate.status_code == 202 and duplicate.json()["scan_id"] == retest_id
        restored = client.get(f"/scans/{retest_id}/report")
        assert restored.status_code == 200 and restored.json()["scan"]["source"] == source
    assert upstream == before


def test_retest_confirms_live_connection_and_shares_provider_budget(comparison_agent):
    run_agent, upstream, _ = comparison_agent
    with run_agent("baseline") as (client, _):
        baseline_id = create_scan(client, endpoint_id="openai", max_tokens=21)
        assert wait_done(client, baseline_id)["state"] == "completed"
    with run_agent("current") as (client, _):
        current_id = create_scan(client, endpoint_id="openai", max_tokens=21)
        assert wait_done(client, current_id)["state"] == "completed"

    payload = retest_payload(baseline_id, current_id)
    with run_agent("current", openai_path=None) as (client, _):
        before = len(upstream)
        assert client.get(f"/scans/{current_id}").status_code == 200
        assert client.post("/scans/retest", json=payload).status_code == 409
        assert len(upstream) == before

    with run_agent("current", openai_path="/alternate/v1", max_requests=2) as (client, _):
        ordinary_id = create_scan(client, endpoint_id="anthropic", models=["stable"])
        assert wait_done(client, ordinary_id)["state"] == "completed"
        before = len(upstream)
        changed = client.post("/scans/retest", json=payload)
        assert changed.status_code == 409
        confirmation = changed.json()
        assert confirmation["code"] == "connection_changed"
        assert any(change["field"] == "endpoint.base_url" for change in confirmation["changes"])
        assert KEY not in changed.text and len(upstream) == before

        wrong_confirmation = client.post(
            "/scans/retest",
            json={**payload, "models": ["regress"], "confirmation": confirmation["confirmation"]},
        )
        assert wrong_confirmation.status_code == 409 and len(upstream) == before
        different_request = client.post(
            "/scans/retest",
            json={
                **payload,
                "request_id": str(uuid4()),
                "confirmation": confirmation["confirmation"],
            },
        )
        assert different_request.status_code == 409 and len(upstream) == before

        confirmed = client.post(
            "/scans/retest", json={**payload, "confirmation": confirmation["confirmation"]}
        )
        assert confirmed.status_code == 202
        retest_id = confirmed.json()["scan_id"]
        finished = wait_done(client, retest_id)
        assert finished["state"] == "failed"
        assert finished["progress"]["finished"] == finished["progress"]["skipped"] == 1
        ((method, path, request_body),) = upstream[before:]
        assert method == "POST" and path == "/alternate/v1/chat/completions"
        assert request_body["model"] in payload["models"]
        assert request_body["max_tokens"] == 21


@pytest.fixture
def diagnostic_agent(tmp_path):
    requests = []
    release_timeout = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, status, document):
            body = json.dumps(document).encode("utf-8")
            try:
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

        def do_GET(self):
            requests.append(("GET", self.path, None))
            self.respond(404, {"error": {"message": "Synthetic model directory unavailable"}})

        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append(("POST", self.path, payload))
            model = payload["model"]
            if model == "timeout":
                release_timeout.wait(5)
            if model == "bad-auth":
                self.respond(401, {"error": {"message": "Synthetic authentication failure"}})
            elif model == "rate-limit":
                self.respond(429, {"error": {"message": "Synthetic rate limit"}})
            else:
                content = "   " if model == "empty" else "pong"
                self.respond(
                    200, {"choices": [{"message": {"role": "assistant", "content": content}}]}
                )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{server.server_port}"
        config = tmp_path / "diagnostic.toml"
        config.write_text(
            f'''[[providers]]
name = "p"
retries = 2
concurrency = 1
timeout = 0.5
scan_timeout = 20
max_requests = 6
[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
profile = "chat-basic"
max_tokens = 24
[[providers]]
name = "q"
retries = 2
max_requests = 12
[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
[[providers.endpoints]]
id = "anthropic"
protocol = "anthropic_messages"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
models = ["same", "same", "other"]
''',
            encoding="utf-8",
        )
        env = {**os.environ, "SCOUT_PROCESS_TEST_KEY": KEY}
        with demo.running_agent_process(config, env, ["--data-dir", str(tmp_path / "data")]) as (
            client,
            _,
        ):
            yield client, requests
    finally:
        release_timeout.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_diagnostic_preview_does_not_create_tasks_or_spend_budget(diagnostic_agent):
    client, requests = diagnostic_agent

    def remaining():
        response = client.get("/providers")
        response.raise_for_status()
        return {
            item["provider_id"]: item["requests_remaining"] for item in response.json()["providers"]
        }

    before = remaining()
    quick = {"provider_id": "p", "mode": "quick", "models": ["good"]}
    assert (
        client.post("/scans/preview", json=quick, headers={"Authorization": ""}).status_code == 401
    )
    response = client.post("/scans/preview", json=quick)
    response.raise_for_status()
    preview = response.json()
    assert preview["mode"] == "quick" and preview["operation"] == "scan"
    assert preview["endpoints"] == [
        {
            "id": "openai",
            "protocol": "openai_chat",
            "max_tokens": 24,
            "target_count": 1,
            "source": "manual",
        }
    ]
    assert preview["target_count"] == preview["max_requests"] == preview["request_upper_bound"] == 1
    assert preview["effective_max_requests"] == 1
    assert preview["requests_remaining"] == before["p"]
    assert preview["retries"] == 0 and preview["timeout"] == 0.5 and preview["scan_timeout"] == 20
    assert KEY not in response.text

    batch = {"provider_id": "q", "endpoint_id": "all", "mode": "batch"}
    response = client.post("/scans/preview", json=batch)
    response.raise_for_status()
    preview = response.json()
    assert preview["target_count"] is None
    assert preview["endpoints"] == [
        {
            "id": "openai",
            "protocol": "openai_chat",
            "max_tokens": 1,
            "target_count": None,
            "source": "api",
        },
        {
            "id": "anthropic",
            "protocol": "anthropic_messages",
            "max_tokens": 1,
            "target_count": 2,
            "source": "manual",
        },
    ]
    assert preview["retries"] == 2

    response = client.post(
        "/scans/preview",
        json={
            **batch,
            "model_selections": {"openai": ["same", "same"], "anthropic": ["same", "other"]},
        },
    )
    response.raise_for_status()
    preview = response.json()
    assert preview["target_count"] == 3
    assert preview["request_upper_bound"] == 9
    assert preview["effective_max_requests"] == 12
    assert [endpoint["target_count"] for endpoint in preview["endpoints"]] == [1, 2]

    for invalid in (
        {**quick, "operation": "models"},
        {**quick, "models": []},
        {**quick, "models": ["good", "bad-auth"]},
        {**quick, "max_requests": 2},
        {**quick, "provider_id": "q", "endpoint_id": "anthropic", "models": None},
        {**quick, "models": None, "model_selections": {"openai": ["good"]}},
        {**quick, "provider_id": "q", "endpoint_id": "all"},
    ):
        assert client.post("/scans/preview", json=invalid).status_code == 422
        assert client.post("/scans", json=invalid).status_code == 422
    assert client.get("/scans").json()["total"] == 0
    assert remaining() == before
    assert not requests


def test_quick_diagnostic_bypasses_missing_directory_and_keeps_evidence(diagnostic_agent):
    client, requests = diagnostic_agent
    discovery_id = create_scan(client, operation="models", max_requests=1)
    assert wait_done(client, discovery_id)["state"] == "failed"
    assert requests == [("GET", "/v1/models", None)]
    assert client.get(f"/scans/{discovery_id}/report").json()["discovery"][0]["http_status"] == 404

    diagnostics = (
        ("good", 200, "OK", None),
        ("bad-auth", 401, "UNAUTHORIZED", None),
        ("rate-limit", 429, "RATE_LIMITED", None),
        ("timeout", None, "TIMEOUT", None),
        ("empty", 200, "BAD_RESPONSE", "empty_text"),
    )
    for index, (model, http_status, category, diagnostic) in enumerate(diagnostics, start=1):
        scan_id = create_scan(client, mode="quick", models=[model], max_tokens=48)
        finished = wait_done(client, scan_id)
        assert finished["state"] == "completed" and finished["progress"]["finished"] == 1
        assert finished["mode"] == "quick"
        response = client.get(f"/scans/{scan_id}/report")
        response.raise_for_status()
        report = response.json()
        assert report["scan"]["requests_sent"] == 1
        assert report["scan"]["policy"]["max_requests"] == 1
        assert report["scan"]["policy"]["retries"] == 0
        assert report["discovery"][0]["source"] == "manual"
        (result,) = report["results"]
        assert result["model"] == model and result["attempts"] == 1
        assert result["http_status"] == http_status
        assert result["error_category"] == category and result["diagnostic_code"] == diagnostic
        assert result["status"] == ("available" if category == "OK" else "unavailable")
        assert len(requests) == index + 1
        method, path, payload = requests[-1]
        assert (method, path) == ("POST", "/v1/chat/completions")
        assert payload["model"] == model and payload["max_tokens"] == 48

    response = client.post(
        "/scans/preview", json={"provider_id": "p", "mode": "quick", "models": ["good"]}
    )
    response.raise_for_status()
    exhausted = response.json()
    assert exhausted["max_requests"] == 1
    assert exhausted["requests_remaining"] == exhausted["effective_max_requests"] == 0
    assert exhausted["request_upper_bound"] == 0
    assert len(requests) == 6


@pytest.fixture
def streaming_agent(tmp_path):
    requests = []
    holds = {
        model: (threading.Event(), threading.Event())
        for model in ("mock-stream-hold", "mock-stream-cancel")
    }

    class Handler(MockHandler):
        def handle_model(self, body):
            requests.append((self.path, self.headers.get("Accept"), body))
            super().handle_model(body)

        def before_stream_terminal(self, model):
            if model in holds:
                sent, release = holds[model]
                sent.set()
                release.wait(10)

    with running_mock(handler=Handler) as base:
        config = tmp_path / "streaming.toml"
        config.write_text(
            f'''[[providers]]
name = "p"
retries = 0
concurrency = 1
[[providers.endpoints]]
id = "chat"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
''',
            encoding="utf-8",
        )
        env = {**os.environ, "SCOUT_PROCESS_TEST_KEY": KEY}
        data_dir = tmp_path / "data"

        def run_agent():
            return demo.running_agent_process(config, env, ["--data-dir", str(data_dir)])

        try:
            yield run_agent, requests, holds
        finally:
            for _, release in holds.values():
                release.set()


def test_stream_agent_reports_empty_disconnect_and_size_limit(streaming_agent):
    run_agent, requests, _ = streaming_agent
    cases = {
        "mock-stream-ok": None,
        "mock-stream-empty": "empty_stream",
        "mock-stream-abrupt": "stream_interrupted",
        "mock-stream-eof": "stream_interrupted",
        "mock-stream-limit": "stream_limit_exceeded",
    }
    with run_agent() as (client, _):
        request = {"provider_id": "p", "capability": "stream", "models": list(cases)}
        preview = client.post("/scans/preview", json=request)
        preview.raise_for_status()
        budget = preview.json()
        assert budget["capability"] == "stream"
        assert budget["target_count"] == budget["request_upper_bound"] == len(cases)
        assert not requests
        scan_id = create_scan(client, capability="stream", models=list(cases), max_requests=5)
        completed = wait_done(client, scan_id)
        assert completed["state"] == "completed" and completed["progress"]["finished"] == 5
        assert completed["capability"] == "stream"
        report = client.get(f"/scans/{scan_id}/report")
        report.raise_for_status()
        assert "合成文本" not in report.text and KEY not in report.text
        document = report.json()
        assert document["scan"]["requests_sent"] == 5
        assert document["summary"]["available"] == 1 and document["summary"]["unavailable"] == 4
        for result in document["results"]:
            diagnostic = cases[result["model"]]
            assert result["diagnostic_code"] == diagnostic
            assert result["status"] == ("unavailable" if diagnostic else "available")
            assert result["http_status"] == 200 and result["attempts"] == 1
            assert result["capability"] == "stream"
        assert len(requests) == 5 and {body["model"] for _, _, body in requests} == cases.keys()
        for path, accept, body in requests:
            assert path == "/v1/chat/completions" and accept == "text/event-stream"
            assert body["stream"] is True

    with run_agent() as (client, _):
        assert client.get(f"/scans/{scan_id}").json()["capability"] == "stream"
        assert client.get(f"/scans/{scan_id}/report").json() == document
    assert len(requests) == 5


def test_stream_agent_waits_for_terminal_event_and_can_cancel_after_text(streaming_agent):
    run_agent, requests, holds = streaming_agent
    with run_agent() as (client, _):
        for model, expected in (
            ("mock-stream-hold", "available"),
            ("mock-stream-cancel", "cancelled"),
        ):
            scan_id = create_scan(client, mode="quick", capability="stream", models=[model])
            sent, release = holds[model]
            assert sent.wait(5), "Mock did not emit the text delta"
            deadline = time.monotonic() + 0.25
            while time.monotonic() < deadline:
                live = client.get(f"/scans/{scan_id}").json()
                assert live["state"] == "running" and live["progress"]["finished"] == 0
                assert client.get(f"/scans/{scan_id}/report").status_code == 409
                time.sleep(0.01)
            if expected == "cancelled":
                assert client.post(f"/scans/{scan_id}/cancel").status_code == 200
            else:
                release.set()
            complete = wait_done(client, scan_id)
            assert complete["state"] == ("cancelled" if expected == "cancelled" else "completed")
            report = client.get(f"/scans/{scan_id}/report").json()
            assert report["scan"]["requests_sent"] == 1
            (row,) = report["results"]
            assert (row["model"], row["status"], row["attempts"]) == (model, expected, 1)
            assert row["capability"] == "stream"
            release.set()
    assert [body["model"] for _, _, body in requests] == list(holds)


@pytest.fixture
def capability_agent(tmp_path):
    requests = []

    class Handler(MockHandler):
        def do_GET(self):
            requests.append((self.path, None))
            super().do_GET()

        def handle_model(self, body):
            requests.append((self.path, body))
            super().handle_model(body)

    with running_mock(handler=Handler) as base:
        config = tmp_path / "capability.toml"
        config.write_text(
            f'''[[providers]]
name = "p"
retries = 0
concurrency = 1
[[providers.endpoints]]
id = "chat"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_PROCESS_TEST_KEY"
profile = "chat-basic"
''',
            encoding="utf-8",
        )
        env = {**os.environ, "SCOUT_PROCESS_TEST_KEY": KEY}
        data_dir = tmp_path / "data"

        def run_agent():
            return demo.running_agent_process(config, env, ["--data-dir", str(data_dir)])

        yield run_agent, requests


def test_structured_agent_preserves_budget_history_and_exports(capability_agent):
    run_agent, requests = capability_agent
    ok = "mock-structured-ok"
    wrong = "mock-structured-wrong-type"
    invalid = "mock-structured-invalid"
    diagnostics = {ok: None, wrong: "schema_mismatch", invalid: "invalid_structured_json"}

    with run_agent() as (client, _):
        preview = client.post(
            "/scans/preview",
            json={"provider_id": "p", "capability": "structured", "models": list(diagnostics)},
        )
        preview.raise_for_status()
        budget = preview.json()
        assert budget["capability"] == "structured"
        assert budget["target_count"] == budget["request_upper_bound"] == len(diagnostics)
        assert not requests and not client.get("/scans").json()["scans"]

        baseline_id = create_scan(client, models=[ok, wrong])
        baseline = wait_done(client, baseline_id)
        assert baseline["state"] == "completed"
        assert baseline.get("capability", "text") == "text"
        assert len(requests) == 2 and all("response_format" not in body for _, body in requests)

        scan_id = create_scan(client, capability="structured", models=list(diagnostics))
        summary = wait_done(client, scan_id)
        assert summary["state"] == "completed" and summary["capability"] == "structured"
        assert summary["progress"]["finished"] == 3
        report_url = f"/scans/{scan_id}/report"
        report = client.get(report_url)
        report.raise_for_status()
        document = report.json()
        assert document["scan"]["requests_sent"] == 3
        assert document["summary"]["available"] == 1 and document["summary"]["unavailable"] == 2
        assert KEY not in report.text and "synthetic-private-structured-text" not in report.text
        for row in document["results"]:
            assert row["diagnostic_code"] == diagnostics[row["model"]]
            assert row["capability"] == "structured"
            assert row["attempts"] == 1 and row["http_status"] == 200
        assert len(requests) == 5
        assert all("response_format" in body for _, body in requests[2:])

    with run_agent() as (client, _):
        assert client.get(f"/scans/{scan_id}").json()["capability"] == "structured"
        assert client.get(report_url).json() == document
        comparison = compare(client, baseline_id, scan_id, "chat")
        comparison.raise_for_status()
        differences = comparison.json()
        assert differences["capabilities"] == {"baseline": "text", "current": "structured"}
        assert {row["verification"] for row in differences["rows"]} == {"not_tested"}

        csv_download = client.get(report_url, params={"format": "csv"})
        csv_download.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(csv_download.content.decode("utf-8-sig"))))
        assert {row["探测配置"] for row in rows} == {"chat-basic (structured)"}
        assert {row["模型"] for row in rows} == diagnostics.keys()

        xlsx_download = client.get(report_url, params={"format": "xlsx"})
        xlsx_download.raise_for_status()
        with io.BytesIO(xlsx_download.content) as data:
            workbook = load_workbook(data, read_only=True)
            try:
                heading, *lines = workbook.active.values
            finally:
                workbook.close()
        assert {line[heading.index("探测配置")] for line in lines} == {"chat-basic (structured)"}
        assert {line[heading.index("模型")] for line in lines} == diagnostics.keys()
    assert len(requests) == 5


def test_tool_agent_records_diagnostics_and_history_without_tool_execution(capability_agent):
    run_agent, requests = capability_agent
    valid = "mock-tool-ok"
    expected = {
        valid: None,
        "mock-tool-wrong-name": "unexpected_tool_name",
        "mock-tool-wrong-args": "invalid_tool_arguments",
        "mock-tool-none": "no_tool_call",
        "mock-tool-multiple": "unexpected_tool_call_count",
    }
    payload = {"provider_id": "p", "capability": "tool", "models": list(expected)}

    with run_agent() as (client, _):
        preview = client.post("/scans/preview", json=payload)
        preview.raise_for_status()
        budget = preview.json()
        assert budget["capability"] == "tool"
        assert budget["target_count"] == budget["request_upper_bound"] == len(expected)
        assert not requests and not client.get("/scans").json()["scans"]

        baseline = create_scan(client, models=[valid])
        assert wait_done(client, baseline).get("capability", "text") == "text"
        ((_, baseline_body),) = requests
        assert "tools" not in baseline_body and "tool_choice" not in baseline_body

        submitted = client.post("/scans", json=payload)
        assert submitted.status_code == 202
        scan_id = submitted.json()["scan_id"]
        task = wait_done(client, scan_id)
        assert task["state"] == "completed" and task["capability"] == "tool"
        assert task["progress"]["finished"] == len(expected)
        report_url = f"/scans/{scan_id}/report"
        report = client.get(report_url)
        report.raise_for_status()
        original = report.json()
        assert original["scan"]["requests_sent"] == len(expected)
        assert original["summary"]["available"] == 1
        assert {row["model"]: row["diagnostic_code"] for row in original["results"]} == expected
        assert all(
            (row["capability"], row["attempts"], row["http_status"]) == ("tool", 1, 200)
            for row in original["results"]
        )
        assert KEY not in report.text and "synthetic-private-tool" not in report.text
        assert "synthetic_private_" not in report.text
        assert len(requests) == len(expected) + 1
        for path, body in requests[1:]:
            assert path == "/v1/chat/completions"
            assert [message["role"] for message in body["messages"]] == ["user"]
            assert body["tools"][0]["function"]["name"] == "scout_tool_probe"

    with run_agent() as (client, _):
        assert client.get(f"/scans/{scan_id}").json()["capability"] == "tool"
        assert client.get(report_url).json() == original
        comparison = compare(client, baseline, scan_id, "chat")
        comparison.raise_for_status()
        assert comparison.json()["capabilities"] == {"baseline": "text", "current": "tool"}
        assert {row["verification"] for row in comparison.json()["rows"]} == {"not_tested"}

        csv_download = client.get(report_url, params={"format": "csv"})
        csv_download.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(csv_download.content.decode("utf-8-sig"))))
        assert {row["探测配置"] for row in rows} == {"chat-basic (tool)"}
        assert {row["模型"] for row in rows} == expected.keys()

        xlsx_download = client.get(report_url, params={"format": "xlsx", "scope": "passed"})
        xlsx_download.raise_for_status()
        with io.BytesIO(xlsx_download.content) as data:
            workbook = load_workbook(data, read_only=True)
            try:
                heading, successful = workbook.active.values
            finally:
                workbook.close()
        assert successful[heading.index("探测配置")] == "chat-basic (tool)"
        assert successful[heading.index("模型")] == valid
    assert len(requests) == len(expected) + 1
