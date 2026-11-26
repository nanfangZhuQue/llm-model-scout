"""Accept the built wheel in an isolated temporary venv, outside the checkout."""

import argparse
import csv
import hashlib
import importlib.util
import io
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.error
import urllib.request
import venv
from collections import Counter
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROTOCOL_PATHS = {
    "openai_chat": "/v1/chat/completions",
    "anthropic_messages": "/v1/messages",
    "openai_responses": "/v1/responses",
}
CASES = {
    "text": ("mock-ok", "mock-missing", None),
    "stream": ("mock-stream-ok", "mock-stream-empty", "empty_stream"),
    "structured": ("mock-structured-ok", "mock-structured-wrong-type", "schema_mismatch"),
    "tool": ("mock-tool-ok", "mock-tool-wrong-args", "invalid_tool_arguments"),
}
PROVIDER_KEY = "synthetic-acceptance-key"


def load_fixture(path):
    spec = importlib.util.spec_from_file_location(Path(path).stem, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def mock_upstream():
    fixture = load_fixture("examples/mock_server.py")
    calls = []

    class MockHandler(fixture.MockHandler):
        def do_GET(self):
            calls.append((self.path, None))
            super().do_GET()

        def handle_model(self, body):
            calls.append((self.path, body))
            super().handle_model(body)

        def wait_for_model(self, model):
            if model == "mock-slow":
                self.server.slow_entered.set()
                self.server.release.wait(timeout=20)

    with ThreadingHTTPServer(("127.0.0.1", 0), MockHandler) as server:
        server.slow_entered = threading.Event()
        server.release = threading.Event()
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield server, calls
        finally:
            server.release.set()
            server.shutdown()
            thread.join()


def run(command, cwd, env, label, timeout=300):
    result = subprocess.run(
        command, cwd=cwd, env=env, capture_output=True, text=True, encoding="utf-8", timeout=timeout
    )
    if result.returncode:
        raise RuntimeError(f"{label} failed with exit code {result.returncode}")
    return result.stdout


def write_registry(config, port, protocols):
    provider = '[[providers]]\nname="acceptance"\nretries=0\nconcurrency=1\nmax_requests=100\n'
    endpoints = [
        f'''[[providers.endpoints]]
id="{protocol}"
protocol="{protocol}"
base_url="http://127.0.0.1:{port}/v1"
api_key_env="ACCEPTANCE_PROVIDER_KEY"
profile="chat-basic"
max_tokens=128
'''
        for protocol in protocols
    ]
    config.write_text(provider + "\n".join(endpoints), encoding="utf-8")


@contextmanager
def installed_agent(python, work, env, config, data_dir):
    token = secrets.token_urlsafe(32)
    settings = {**env, "ACCEPTANCE_AGENT_TOKEN": token}
    with socket.socket() as reserve:
        reserve.bind(("127.0.0.1", 0))
        port = reserve.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def request(path, body=None, auth=True):
        headers = {"Authorization": f"Bearer {token}"} if auth else {}
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(origin + path, data=data, headers=headers)
        try:
            with opener.open(req, timeout=5) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            with error:
                return error.code, error.read(), error.headers

    process = subprocess.Popen(
        [
            str(python),
            "-I",
            "-m",
            "llm_model_scout",
            "agent",
            "--config",
            str(config),
            "--data-dir",
            str(data_dir),
            "--port",
            str(port),
            "--token-env",
            "ACCEPTANCE_AGENT_TOKEN",
        ],
        cwd=work,
        env=settings,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("Installed Agent exited during startup")
            try:
                if request("/health", auth=False)[0] == 200:
                    break
            except (urllib.error.URLError, TimeoutError):
                pass
            time.sleep(0.05)
        else:
            raise RuntimeError("Installed Agent startup timed out")
        yield request, process, origin, token
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def api_json(request, path, body=None, expected=200):
    status, content, _ = request(path, body)
    assert status == expected, f"{path} returned HTTP {status}, expected {expected}"
    assert_no_synthetic_payload(content)
    return json.loads(content)


def assert_no_synthetic_payload(content):
    for private in (PROVIDER_KEY, "synthetic-private", "synthetic_private_"):
        assert private.encode() not in content, "Synthetic sensitive content was retained"


def assert_capability_requests(calls, capability, models, protocols):
    assert Counter((path, body and body["model"]) for path, body in calls) == Counter(
        (PROTOCOL_PATHS[protocol], model) for protocol in protocols for model in models
    )
    for _, body in calls:
        assert body["stream"] is (capability == "stream")
        assert ("tools" in body) is (capability == "tool")
        assert bool(body.keys() & {"response_format", "text", "output_config"}) is (
            capability == "structured"
        )
        assert [message["role"] for message in body.get("messages", [])] in ([], ["user"])


def accept_cli(python, work, env, config, calls):
    for capability, (model, _, _) in CASES.items():
        before = len(calls)
        document = json.loads(
            run(
                [
                    str(python),
                    "-I",
                    "-m",
                    "llm_model_scout",
                    "verify",
                    "--config",
                    str(config),
                    "--endpoint",
                    "openai_chat",
                    "--model",
                    model,
                    "--capability",
                    capability,
                    "--max-requests",
                    "1",
                    "--fail-on-unavailable",
                ],
                work,
                env,
                f"CLI {capability} verification",
            )
        )
        (result,) = document["results"]
        assert result["status"] == "available" and result.get("capability", "text") == capability
        assert result["attempts"] == document["scan"]["requests_sent"] == 1
        assert_capability_requests(calls[before:], capability, [model], ["openai_chat"])


def accept_scans(request, calls):
    reports = {}
    for capability, (valid, invalid, diagnostic) in CASES.items():
        before = len(calls)
        payload = {
            "provider_id": "acceptance",
            "endpoint_id": "all",
            "models": [valid, invalid],
            "capability": capability,
            "max_requests": 8,
        }
        preview = api_json(request, "/scans/preview", payload)
        assert preview["capability"] == capability
        assert preview["target_count"] == preview["request_upper_bound"] == 6
        assert preview["retries"] == 0 and len(calls) == before
        scan_id = api_json(request, "/scans", payload, expected=202)["scan_id"]
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            state = api_json(request, f"/scans/{scan_id}")
            if state["state"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.03)
        else:
            raise RuntimeError(f"{capability} scan did not finish")
        assert state["state"] == "completed" and state.get("capability", "text") == capability
        report = api_json(request, f"/scans/{scan_id}/report")
        assert report["scan"]["requests_sent"] == report["summary"]["total"] == 6
        assert report["summary"]["available"] == report["summary"]["unavailable"] == 3
        assert {(row["protocol"], row["model"]) for row in report["results"]} == {
            (protocol, model) for protocol in PROTOCOL_PATHS for model in (valid, invalid)
        }
        for row in report["results"]:
            assert row["attempts"] == 1 and row.get("capability", "text") == capability
            if row["model"] == valid:
                assert row["status"] == "available" and row["http_status"] == 200
            else:
                assert row["status"] == "unavailable" and row["diagnostic_code"] == diagnostic
                assert row["error_category"] == (
                    "MODEL_NOT_FOUND" if capability == "text" else "BAD_RESPONSE"
                )
        assert_capability_requests(calls[before:], capability, (valid, invalid), PROTOCOL_PATHS)
        reports[scan_id] = report
    return reports


def accept_history(request, reports, partial_id, work):
    history = api_json(request, "/scans?provider_id=acceptance&limit=3")
    next_page = api_json(request, "/scans?provider_id=acceptance&limit=3&offset=3")
    assert history["total"] == next_page["total"] == 5
    assert {item["scan_id"] for item in history["scans"] + next_page["scans"]} == {
        *reports,
        partial_id,
    }
    for scan_id, report in reports.items():
        detail = api_json(request, f"/scans/{scan_id}")
        assert detail["state"] == "completed" and detail["report_available"] is True
        assert api_json(request, f"/scans/{scan_id}/report") == report
    partial = api_json(request, f"/scans/{partial_id}")
    assert partial["state"] == "interrupted" and partial["report_partial"] is True
    report = api_json(request, f"/scans/{partial_id}/report")
    assert report["scan"]["partial"] is True and report["scan"]["requests_sent"] is None
    assert [(row["model"], row["status"]) for row in report["results"]] == [
        ("mock-ok", "available")
    ]
    workbooks = {}
    for scan_id, document in {**reports, partial_id: report}.items():
        path = f"/scans/{scan_id}/report"
        status, body, headers = request(path + "?format=csv")
        assert status == 200 and body.startswith(b"\xef\xbb\xbf")
        assert "attachment" in headers["Content-Disposition"]
        assert_no_synthetic_payload(body)
        csv_rows = list(csv.DictReader(io.StringIO(body.decode("utf-8-sig"))))
        expected = []
        for row in document["results"]:
            capability = row.get("capability", "text")
            probe = row["probe_id"] if capability == "text" else f"{row['probe_id']} ({capability})"
            expected.append([row["endpoint_id"], row["model"], row["status"], probe])
        columns = ("入口ID", "模型", "模型状态", "探测配置")
        assert sorted([row[col] for col in columns] for row in csv_rows) == sorted(expected)
        for scope in ("all", "passed"):
            status, body, headers = request(path + f"?format=xlsx&scope={scope}")
            assert status == 200 and "attachment" in headers["Content-Disposition"]
            filename = f"{scan_id}-{scope}.xlsx"
            (work / filename).write_bytes(body)
            workbooks[filename] = [
                [endpoint, model, "通过" if state == "available" else "未通过", probe]
                for endpoint, model, state, probe in expected
                if scope == "all" or state == "available"
            ]
    return workbooks


def assert_workbooks(python, work, env, workbooks):
    (work / "workbook-expectations.json").write_text(json.dumps(workbooks), encoding="utf-8")
    # Read the reports with the dependency installed in the same new environment.
    script = """
import json
from pathlib import Path
from openpyxl import load_workbook

for filename, expected in json.loads(Path('workbook-expectations.json').read_text()).items():
    with open(filename, 'rb') as stream:
        workbook = load_workbook(stream)
        try:
            sheet = workbook.active
            header, *rows = sheet.values
            columns = [header.index(c) for c in ('入口ID', '模型', '模型状态', '探测配置')]
            assert sorted([row[index] for index in columns] for row in rows) == sorted(expected)
            assert sheet.auto_filter.ref == sheet.dimensions
            assert 'synthetic-acceptance-key' not in str(rows)
        finally:
            workbook.close()
"""
    run([str(python), "-I", "-c", script], work, env, "installed XLSX readback")


def accept_browser(origin, token, screenshots, evidence, calls):
    from playwright.sync_api import expect, sync_playwright

    module = load_fixture("scripts/test_web_ui.py")
    screenshots.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        instance = playwright.chromium.launch(headless=True)
        try:
            evidence["chromium"] = instance.version
            page = instance.new_page(accept_downloads=True)
            module.exercise(page, origin, token, screenshots)
            page.get_by_label("Agent 会话 Token", exact=True).fill(token)
            page.get_by_role("button", name="连接工作台", exact=True).click()
            expect(page.locator("#workspace")).to_be_visible()
            page.get_by_label("验收方式").select_option("quick")
            page.locator("#endpoint").select_option("openai_chat")
            for capability in CASES:
                model = f"mock-browser-{capability}"
                before = len(calls)
                page.get_by_label("探测能力").select_option(capability)
                page.get_by_label("指定模型 ID").fill(model)
                expect(page.locator("#scan-preview")).to_contain_text("本次最多 1 次请求")
                assert len(calls) == before
                page.get_by_role("button", name="验证指定模型", exact=True).click()
                result = page.locator("#result-rows tr").filter(has_text=model)
                expect(result.get_by_role("cell").nth(1)).to_have_text("通过")
                assert_capability_requests(calls[before:], capability, [model], ["openai_chat"])
            assert module.download_workbook(page, "下载 XLSX（仅通过）", "-仅通过.xlsx") == [
                ("mock-browser-tool", "通过")
            ]
        finally:
            instance.close()


def accept(wheel, expected_version, evidence, browser, screenshots, wheelhouse=None):
    with tempfile.TemporaryDirectory(prefix="scout-wheel-acceptance-") as temporary:
        base = Path(temporary)
        environment = base / "venv"
        work = base / "outside-checkout"
        work.mkdir()
        evidence["stage"] = "create_clean_environment"
        assert not work.resolve().is_relative_to(ROOT), "Acceptance must run outside the checkout"
        venv.EnvBuilder(with_pip=True, system_site_packages=False).create(environment)
        python = environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        env = {
            name: value
            for name, value in os.environ.items()
            if name.upper() not in ("PYTHONPATH", "PYTHONHOME", "PYTHONUSERBASE")
            and not name.upper().startswith("SCOUT_")
        }
        evidence["stage"] = "install_wheel"
        sources = ["--no-index", "--find-links", str(wheelhouse.resolve())] if wheelhouse else []
        run(
            [
                str(python),
                "-I",
                "-m",
                "pip",
                "install",
                str(wheel) + "[agent]",
                "--timeout",
                "30",
                "--retries",
                "1",
                *sources,
            ],
            work,
            env,
            "wheel install",
        )
        evidence["stage"] = "verify_import_origin"
        info = json.loads(
            run(
                [
                    str(python),
                    "-I",
                    "-c",
                    "import json,sys,llm_model_scout; "
                    "print(json.dumps({'version':llm_model_scout.__version__,"
                    "'module':llm_model_scout.__file__,'prefix':sys.prefix,"
                    "'base_prefix':sys.base_prefix}))",
                ],
                work,
                env,
                "import proof",
            )
        )
        assert info["version"] == expected_version
        assert Path(info["module"]).resolve().is_relative_to(environment.resolve())
        assert info["prefix"] != info["base_prefix"]
        evidence["installed"] = info
        assert (
            run(
                [str(python), "-I", "-m", "llm_model_scout", "version"], work, env, "CLI version"
            ).strip()
            == expected_version
        )
        env["ACCEPTANCE_PROVIDER_KEY"] = PROVIDER_KEY
        config = work / "providers.toml"
        data_dir = base / "data"
        with mock_upstream() as (mock, calls):
            write_registry(config, mock.server_port, PROTOCOL_PATHS)
            evidence["stage"] = "installed_cli_capabilities"
            accept_cli(python, work, env, config, calls)

            evidence["stage"] = "installed_agent_capabilities"
            with installed_agent(python, work, env, config, data_dir) as (
                request,
                process,
                _,
                token,
            ):
                assert request("/providers", auth=False)[0] == 401
                assert request("/providers")[0] == 200
                for asset in ("/", "/ui/app.js", "/ui/app.css", "/ui/providers.js", "/favicon.svg"):
                    status, content, headers = request(asset, auth=False)
                    assert status == 200 and content
                    assert "default-src 'self'" in headers["Content-Security-Policy"]
                    assert token.encode() not in content
                reports = accept_scans(request, calls)
                evidence["capabilities"] = {
                    capability: {
                        "cli_verified": True,
                        "agent_protocols": list(PROTOCOL_PATHS),
                        "requests": document["scan"]["requests_sent"],
                        "available": document["summary"]["available"],
                        "unavailable": document["summary"]["unavailable"],
                    }
                    for capability, document in zip(CASES, reports.values(), strict=True)
                }

                evidence["stage"] = "interrupt_after_checkpoint"
                before = len(calls)
                partial_id = api_json(
                    request,
                    "/scans",
                    {
                        "provider_id": "acceptance",
                        "endpoint_id": "openai_chat",
                        "models": ["mock-ok", "mock-slow"],
                        "max_requests": 2,
                    },
                    expected=202,
                )["scan_id"]
                assert mock.slow_entered.wait(timeout=15), "Second model did not enter the fixture"
                pending = api_json(request, f"/scans/{partial_id}")
                assert pending["state"] == "running"
                assert pending["progress"]["finished"] == 1 and pending["progress"]["planned"] == 2
                assert_capability_requests(
                    calls[before:], "text", ["mock-ok", "mock-slow"], ["openai_chat"]
                )
                process.kill()

            mock.release.set()
            evidence["stage"] = "restart_history_and_download"
            before = len(calls)
            with installed_agent(python, work, env, config, data_dir) as (request, _, _, _):
                workbooks = accept_history(request, reports, partial_id, work)
            assert len(calls) == before, "History recovery or download called the upstream"
            assert_workbooks(python, work, env, workbooks)
            evidence["history"] = {
                "completed_reports": len(reports),
                "interrupted_reports": 1,
                "restored_checkpoint_results": 1,
                "download_formats": ["json", "csv", "xlsx"],
                "xlsx_scopes": ["all", "passed"],
                "upstream_replay_requests": 0,
            }
            evidence["checks"] = [
                "isolated import",
                "CLI version and four capabilities",
                "Agent startup and authentication",
                "installed UI assets and CSP",
                "four capabilities across three protocols and request budgets",
                "same-directory restart and partial recovery",
                "historical JSON/CSV/XLSX download and workbook readback",
            ]
            if browser:
                evidence["stage"] = "native_browser_on_installed_wheel"
                # The existing browser fixture tests a dual-protocol workbench.
                browser_config = work / "browser.toml"
                write_registry(
                    browser_config, mock.server_port, ("openai_chat", "anthropic_messages")
                )
                mock.release.clear()
                with installed_agent(python, work, env, browser_config, base / "browser-data") as (
                    _,
                    _,
                    origin,
                    token,
                ):
                    accept_browser(origin, token, screenshots, evidence, calls)
                evidence["checks"].append(
                    "native Playwright against installed wheel, four capabilities"
                )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument(
        "--wheelhouse", type=Path, help="Only install dependencies from this folder"
    )
    parser.add_argument("--output", type=Path, default=ROOT / ".scout/acceptance-wheel.json")
    parser.add_argument("--browser", action="store_true")
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    wheel = (args.wheel or ROOT / f"dist/llm_model_scout-{version}-py3-none-any.whl").resolve()
    if not wheel.is_file() or args.output.exists():
        parser.error("Wheel must exist and output must be a new file")
    if args.wheelhouse is not None and not args.wheelhouse.is_dir():
        parser.error("Dependency wheelhouse must be a directory")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "version": version,
        "platform": platform.platform(),
        "python": platform.python_version(),
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "status": "running",
    }
    try:
        accept(
            wheel,
            version,
            evidence,
            args.browser,
            args.output.parent / "wheel-browser",
            args.wheelhouse,
        )
        evidence.update(status="passed", stage="complete")
    except Exception as error:
        evidence.update(status="failed", error=type(error).__name__)
        print(
            f"Acceptance failed during {evidence['stage']}: {type(error).__name__}", file=sys.stderr
        )
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(evidence, stream, ensure_ascii=False, indent=2)
    print(json.dumps(evidence, ensure_ascii=False, indent=2))
    return 0 if evidence["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
