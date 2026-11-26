import asyncio
import csv
import io
import json
import os
import subprocess
import sys

import httpx
import pytest
from typer.testing import CliRunner

from examples.mock_server import MockHandler, running_mock
from llm_model_scout import __version__
from llm_model_scout.cli import app
from llm_model_scout.core.scanner import scan
from llm_model_scout.report import CSV_FIELD_LABELS


def run_cli(command, *arguments):
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCOUT_")}
    env["TEST_SCOUT_KEY"] = "local-test-secret"
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, "-m", "llm_model_scout", command, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        timeout=15,
    )


def cli(server, command="scan", *extra, protocol="openai_chat"):
    return run_cli(
        command,
        "--protocol",
        protocol,
        "--base-url",
        server + "/v1",
        "--api-key-env",
        "TEST_SCOUT_KEY",
        *extra,
    )


@pytest.fixture
def responses_server():
    calls = []

    class RecordingHandler(MockHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization"), None))
            super().do_GET()

        def handle_model(self, body):
            calls.append((self.path, self.headers.get("Authorization"), body))
            super().handle_model(body)

    with running_mock(handler=RecordingHandler) as base:
        yield base, calls


def test_responses_verify_cli(responses_server):
    base, calls = responses_server
    result = cli(
        base,
        "verify",
        "--model",
        "mock-ok",
        "--profile",
        "chat-basic",
        "--max-tokens",
        "96",
        protocol="openai_responses",
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    row = report["results"][0]
    assert row["protocol"] == "openai_responses"
    assert (row["status"], row["attempts"]) == ("available", 1)
    assert calls == [
        (
            "/v1/responses",
            "Bearer local-test-secret",
            {
                "model": "mock-ok",
                "input": "ping",
                "max_output_tokens": 96,
                "stream": False,
                "store": False,
            },
        )
    ]
    assert "local-test-secret" not in result.stdout + result.stderr


def test_responses_and_chat_remain_independent_in_cli_scan(responses_server, tmp_path):
    base, calls = responses_server
    config = tmp_path / "responses.toml"
    config.write_text(
        f'''[[providers]]
name = "local"
retries = 0
concurrency = 1
[[providers.endpoints]]
id = "chat"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "TEST_SCOUT_KEY"
profile = "chat-basic"
max_tokens = 96
models = ["mock-ok"]
[[providers.endpoints]]
id = "responses"
protocol = "openai_responses"
base_url = "{base}/v1"
api_key_env = "TEST_SCOUT_KEY"
profile = "chat-basic"
max_tokens = 96
models = ["mock-ok", "mock-responses-refusal", "mock-responses-empty",
    "mock-responses-incomplete", "mock-responses-error"]
''',
        encoding="utf-8",
    )
    result = run_cli("scan", "--config", str(config), "--endpoint", "all", "--verbose")
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["summary"]["total"] == 6
    assert report["summary"]["available"] == 2
    assert report["summary"]["unavailable"] == 4
    assert {
        (row["endpoint_id"], row["protocol"])
        for row in report["results"]
        if row["model"] == "mock-ok"
    } == {
        ("chat", "openai_chat"),
        ("responses", "openai_responses"),
    }
    diagnostics = {
        row["model"]: (row["error_category"], row["diagnostic_code"])
        for row in report["results"]
        if row["status"] == "unavailable"
    }
    assert diagnostics == {
        "mock-responses-refusal": ("BAD_RESPONSE", "refusal"),
        "mock-responses-empty": ("BAD_RESPONSE", "empty_text"),
        "mock-responses-incomplete": ("BAD_RESPONSE", "incomplete_response"),
        "mock-responses-error": ("BAD_RESPONSE", "response_error"),
    }
    assert (
        next(row for row in report["results"] if row["model"] == "mock-responses-incomplete")[
            "stop_reason"
        ]
        == "max_output_tokens"
    )
    assert all(row["http_status"] == 200 and row["attempts"] == 1 for row in report["results"])
    assert "已验证 6/6" in result.stderr
    paths = [(path, body["model"]) for path, _, body in calls]
    assert paths.count(("/v1/chat/completions", "mock-ok")) == 1
    assert paths.count(("/v1/responses", "mock-ok")) == 1
    assert {model for path, model in paths if path == "/v1/responses"} == {
        "mock-ok",
        *diagnostics,
    }
    assert len(paths) == 6


@pytest.mark.parametrize(
    ("protocol", "path", "token_field", "auth"),
    [
        ("openai_chat", "/v1/chat/completions", "max_tokens", "Bearer local-test-secret"),
        ("anthropic_messages", "/v1/messages", "max_tokens", None),
        ("openai_responses", "/v1/responses", "max_output_tokens", "Bearer local-test-secret"),
    ],
)
def test_stream_cli_verifies_each_protocol(responses_server, protocol, path, token_field, auth):
    base, calls = responses_server
    result = cli(
        base,
        "verify",
        "--capability",
        "stream",
        "--model",
        "mock-stream-ok",
        "--profile",
        "chat-basic",
        "--max-tokens",
        "32",
        "--max-requests",
        "1",
        protocol=protocol,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    (row,) = report["results"]
    assert (row["protocol"], row["capability"], row["status"], row["attempts"]) == (
        protocol,
        "stream",
        "available",
        1,
    )
    assert report["scan"]["requests_sent"] == 1
    (call,) = calls
    actual_path, actual_auth, body = call
    assert (actual_path, actual_auth, body["stream"], body[token_field]) == (path, auth, True, 32)
    assert "合成文本" not in result.stdout + result.stderr


def test_stream_cli_toml_default_and_text_override(responses_server, tmp_path):
    base, calls = responses_server
    config = tmp_path / "streaming.toml"
    config.write_text(
        f'''[[providers]]
name = "local"
retries = 0
[[providers.endpoints]]
id = "responses"
protocol = "openai_responses"
base_url = "{base}/v1"
api_key_env = "TEST_SCOUT_KEY"
capability = "stream"
models = ["mock-ok"]
''',
        encoding="utf-8",
    )
    for options, expected in (((), "stream"), (("--capability", "text"), "text")):
        result = run_cli("scan", "--config", str(config), *options)
        assert result.returncode == 0, result.stderr
        (row,) = json.loads(result.stdout)["results"]
        assert row.get("capability", "text") == expected and row["status"] == "available"
    assert [body["stream"] for _, _, body in calls] == [True, False]


@pytest.mark.parametrize(
    ("protocol", "path", "format_container", "token_field"),
    [
        ("openai_chat", "/v1/chat/completions", "response_format", "max_tokens"),
        ("openai_responses", "/v1/responses", "text", "max_output_tokens"),
        ("anthropic_messages", "/v1/messages", "output_config", "max_tokens"),
    ],
)
def test_structured_cli_validates_the_schema_on_each_protocol(
    responses_server, protocol, path, format_container, token_field
):
    base, calls = responses_server
    ok = "mock-structured-ok"
    wrong = "mock-structured-wrong-type"
    result = cli(
        base,
        "scan",
        "--capability",
        "structured",
        "--model",
        ok,
        "--model",
        wrong,
        "--concurrency",
        "1",
        "--max-requests",
        "2",
        "--fail-on-unavailable",
        protocol=protocol,
    )
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report["scan"]["requests_sent"] == 2 and len(calls) == 2
    assert {row["model"]: (row["status"], row["diagnostic_code"]) for row in report["results"]} == {
        ok: ("available", None),
        wrong: ("unavailable", "schema_mismatch"),
    }
    for row in report["results"]:
        assert row["protocol"] == protocol and row["capability"] == "structured"
        assert row["attempts"] == 1 and row["http_status"] == 200
    for actual_path, _, body in calls:
        assert actual_path == path and body["stream"] is False
        assert body[token_field] == 128 and "temperature" not in body
        wrapper = body[format_container]
        if format_container == "response_format":
            assert wrapper["type"] == "json_schema"
            declared = wrapper["json_schema"]
        else:
            declared = wrapper["format"]
            assert declared["type"] == "json_schema"
        if protocol != "anthropic_messages":
            assert declared["name"] == "scout_structured_v1" and declared["strict"] is True
        assert declared["schema"] == {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["ok"]},
                "count": {"type": "integer"},
            },
            "required": ["status", "count"],
            "additionalProperties": False,
        }
    assert '"count":1' not in result.stdout and '"count":"1"' not in result.stdout


@pytest.mark.parametrize(
    ("protocol", "path"),
    [
        ("openai_chat", "/v1/chat/completions"),
        ("openai_responses", "/v1/responses"),
        ("anthropic_messages", "/v1/messages"),
    ],
)
def test_tool_cli_validates_instruction_without_follow_up(responses_server, protocol, path):
    base, calls = responses_server
    expected = {
        "mock-tool-ok": None,
        "mock-tool-wrong-name": "unexpected_tool_name",
        "mock-tool-wrong-args": "invalid_tool_arguments",
        "mock-tool-none": "no_tool_call",
        "mock-tool-multiple": "unexpected_tool_call_count",
    }
    models = [argument for model in expected for argument in ("--model", model)]
    result = cli(
        base,
        "scan",
        "--capability",
        "tool",
        "--retries",
        "0",
        "--max-requests",
        "10",
        "--concurrency",
        "1",
        "--fail-on-unavailable",
        *models,
        protocol=protocol,
    )
    assert result.returncode == 2, result.stderr
    assert "synthetic-private-tool" not in result.stdout + result.stderr
    assert "synthetic_private_" not in result.stdout + result.stderr
    report = json.loads(result.stdout)
    assert report["scan"]["requests_sent"] == len(calls) == len(expected)
    assert report["summary"]["available"] == 1 and report["summary"]["unavailable"] == 4
    for row in report["results"]:
        assert row["protocol"] == protocol and row["capability"] == "tool"
        assert row["diagnostic_code"] == expected[row["model"]]
        assert row["attempts"] == 1 and row["http_status"] == 200
    assert {body["model"] for _, _, body in calls} == expected.keys()
    for actual_path, _, body in calls:
        assert actual_path == path and body["stream"] is False
        token_field = "max_output_tokens" if protocol == "openai_responses" else "max_tokens"
        assert body[token_field] == 128 and "temperature" not in body
        (definition,) = body["tools"]
        if protocol == "openai_chat":
            assert definition["type"] == "function"
            definition = definition["function"]
            assert body["tool_choice"] == {
                "type": "function",
                "function": {"name": "scout_tool_probe"},
            }
        elif protocol == "openai_responses":
            assert definition["type"] == "function"
            assert body["tool_choice"] == {"type": "function", "name": "scout_tool_probe"}
            assert body["store"] is False and "previous_response_id" not in body
        else:
            assert body["tool_choice"] == {
                "type": "tool",
                "name": "scout_tool_probe",
                "disable_parallel_tool_use": True,
            }
        assert definition["name"] == "scout_tool_probe" and definition["strict"] is True
        if protocol == "openai_responses":
            prompt = body["input"]
        else:
            (message,) = body["messages"]
            assert message["role"] == "user"
            prompt = message["content"]
        assert prompt == 'Call scout_tool_probe exactly once with value "ping" and count 1.'
        if protocol != "anthropic_messages":
            assert body["parallel_tool_calls"] is False
        schema_field = "input_schema" if protocol == "anthropic_messages" else "parameters"
        assert definition[schema_field] == {
            "type": "object",
            "properties": {
                "value": {"type": "string", "enum": ["ping"]},
                "count": {"type": "integer"},
            },
            "required": ["value", "count"],
            "additionalProperties": False,
        }


def test_models_cli(http_server):
    server, requests = http_server
    result = cli(server, "models", "--verbose")
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc["discovery"][0]["models"] == ["bad", "good"]
    assert "发现完成 · 已发现 2 个候选" in result.stderr
    assert "验证阶段" not in result.stderr
    assert len(requests) == 1


def test_scan_json_and_file(http_server, tmp_path):
    server, _ = http_server
    path = tmp_path / "report.json"
    result = cli(server, "scan", "--out", str(path), "--verbose")
    assert result.returncode == 0, result.stderr
    doc = json.loads(result.stdout)
    assert doc == json.loads(path.read_text(encoding="utf-8"))
    assert doc["summary"]["available"] == 1
    assert doc["summary"]["unavailable"] == 1
    out_text = result.stdout + (result.stderr or "") + path.read_text()
    assert "local-test-secret" not in out_text
    assert "已验证 2/2" in result.stderr
    second = cli(server, "scan", "--out", str(path))
    assert second.returncode == 1
    assert json.loads(path.read_text()) == doc


@pytest.mark.parametrize(
    ("mode", "exit_code", "finished"),
    [("complete", 0, 3), ("budget", 1, 1), ("cancel", 130, 1)],
)
def test_verbose_progress_uses_plan_across_endpoints(
    monkeypatch, tmp_path, mode, exit_code, finished
):
    config = tmp_path / "provider.toml"
    config.write_text(
        """[[providers]]
name = "test"
concurrency = 1
retries = 0
[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "https://openai.test/v1"
api_key_env = "TEST_SCOUT_KEY"
[[providers.endpoints]]
id = "anthropic"
protocol = "anthropic_messages"
base_url = "https://anthropic.test/v1"
api_key_env = "TEST_SCOUT_KEY"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEST_SCOUT_KEY", "synthetic-cli-progress-key")

    async def respond(request):
        is_openai = request.url.host == "openai.test"
        if request.method == "GET":
            models = ["ignored", "a"] if is_openai else ["b", "c", "ignored"]
            return httpx.Response(200, json={"data": [{"id": model} for model in models]})
        if is_openai:
            return httpx.Response(
                200, json={"choices": [{"message": {"role": "assistant", "content": "ok"}}]}
            )
        if mode == "cancel":
            await asyncio.Event().wait()
        return httpx.Response(
            200,
            json={
                "type": "message",
                "role": "assistant",
                "content": [{"type": "text", "text": "ok"}],
            },
        )

    async def local_scan(provider, *, on_event, **options):
        task = asyncio.current_task()

        def event(value):
            on_event(value)
            if mode == "cancel" and value["event"] == "result":
                task.cancel()

        return await scan(
            provider, on_event=event, http_transport=httpx.MockTransport(respond), **options
        )

    monkeypatch.setattr("llm_model_scout.cli.scan", local_scan)
    result = CliRunner().invoke(
        app,
        [
            "scan",
            "--config",
            str(config),
            "--endpoint",
            "all",
            "--exclude",
            "^ignored$",
            "--max-requests",
            "3" if mode == "budget" else "5",
            "--verbose",
        ],
    )
    assert result.exit_code == exit_code, result.output
    report = json.loads(result.stdout)
    assert report["summary"]["available"] == finished
    assert "（累计 5）" in result.stderr
    assert "验证阶段 · 已验证 0/3" in result.stderr
    assert f"已验证 {finished}/3" in result.stderr.splitlines()[-1]
    if mode == "budget":
        assert "跳过 2 项" in result.stderr.splitlines()[-1]
    elif mode == "cancel":
        assert "扫描已取消" in result.stderr.splitlines()[-1]


def test_verify_and_failure_exit(http_server):
    server, requests = http_server
    result = cli(server, "verify", "--model", "good", "--profile", "chat-basic")
    assert result.returncode == 0, result.stderr
    assert len(requests) == 1
    assert "temperature" not in requests[0][3]
    assert requests[0][3]["max_tokens"] == 16
    failed = cli(server, "verify", "--model", "bad", "--fail-on-unavailable")
    assert failed.returncode == 2


def test_cli_deadline_writes_partial_report(http_server, tmp_path):
    server, _ = http_server
    result = cli(
        server,
        "scan",
        "--model",
        "slow",
        "--scan-timeout",
        "0.05",
        "--out",
        str(tmp_path / "partial.json"),
    )
    assert result.returncode == 1
    report = json.loads(result.stdout)
    assert report["scan"]["reason"] == "scan_deadline_exceeded"
    assert report["results"][0]["status"] == "cancelled"


def test_help_version_and_config_errors():
    runner = CliRunner()
    assert runner.invoke(app, ["--help"]).exit_code == 0
    assert runner.invoke(app, ["version"]).stdout.strip() == __version__
    result = runner.invoke(app, ["scan", "--protocol", "unknown"])
    assert result.exit_code == 1
    assert "protocol" in result.output


def test_max_tokens_cli_reaches_request_and_report(http_server):
    server, requests = http_server
    result = cli(server, "verify", "--model", "good", "--max-tokens", "128")
    assert result.returncode == 0, result.stderr
    assert requests[0][3]["max_tokens"] == 128
    report = json.loads(result.stdout)
    assert report["provider"]["endpoints"][0]["probe"]["max_tokens"] == 128
    assert report["results"][0]["diagnostic_code"] is None
    assert "suggestion" in report["results"][0]


def test_invalid_max_tokens_sends_no_request(http_server):
    server, requests = http_server
    result = cli(server, "verify", "--model", "good", "--max-tokens", "0")
    assert result.returncode == 1
    assert "max_tokens" in result.stderr
    assert not requests


def test_csv_file_keeps_stdout_json(http_server, tmp_path):
    server, _ = http_server
    path = tmp_path / "report.csv"
    result = cli(server, "scan", "--format", "csv", "--out", str(path))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert len(rows) == report["summary"]["total"] == 2
    for row, item in zip(rows, report["results"], strict=True):
        assert row[CSV_FIELD_LABELS["model"]] == item["model"]
        assert row[CSV_FIELD_LABELS["status"]] == item["status"]
    assert "local-test-secret" not in path.read_text(encoding="utf-8-sig")
    again = cli(server, "scan", "--format", "csv", "--out", str(path))
    assert again.returncode == 1


def test_csv_invalid_combinations_send_no_requests(http_server, tmp_path):
    server, requests = http_server
    cases = [
        ("scan", "--format", "csv"),
        ("scan", "--format", "xlsx", "--out", str(tmp_path / "bad.xlsx")),
        ("models", "--format", "csv", "--out", str(tmp_path / "models.csv")),
    ]
    for command, *arguments in cases:
        result = cli(server, command, *arguments)
        assert result.returncode == 1
    assert not requests
    assert not list(tmp_path.iterdir())


def test_csv_formula_protection_and_json_original(http_server, tmp_path):
    server, _ = http_server
    path = tmp_path / "formula.csv"
    result = cli(server, "verify", "--model", "=1+1", "--format", "csv", "--out", str(path))
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    row = next(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert row[CSV_FIELD_LABELS["model"]] == "'=1+1"
    assert report["results"][0]["model"] == "=1+1"


def test_csv_deadline_partial_report(http_server, tmp_path):
    server, _ = http_server
    path = tmp_path / "partial.csv"
    result = cli(
        server,
        "scan",
        "--model",
        "slow",
        "--scan-timeout",
        "0.05",
        "--format",
        "csv",
        "--out",
        str(path),
    )
    assert result.returncode == 1
    row = next(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert row[CSV_FIELD_LABELS["scan_status"]] == "failed"
    assert row[CSV_FIELD_LABELS["status"]] == "cancelled"
