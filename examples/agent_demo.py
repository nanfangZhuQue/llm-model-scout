"""Run an authenticated local Agent demo; defaults to synthetic upstream endpoints."""

import argparse
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx


@contextmanager
def running_agent(config, environ=None, extra_args=()):
    with running_agent_process(config, environ, extra_args) as (client, _):
        yield client


@contextmanager
def running_agent_process(config, environ=None, extra_args=()):
    env = dict(os.environ if environ is None else environ)
    token = secrets.token_urlsafe(32)
    env["SCOUT_AGENT_DEMO_TOKEN"] = token
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "llm_model_scout",
            "agent",
            *(["--config", str(config)] if config is not None else []),
            "--port",
            str(port),
            "--token-env",
            "SCOUT_AGENT_DEMO_TOKEN",
            *extra_args,
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    )
    try:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{port}",
            trust_env=False,
            headers={"Authorization": f"Bearer {token}"},
            timeout=5,
        ) as client:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("Agent process exited before becoming ready")
                try:
                    if client.get("/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
            else:
                raise RuntimeError("Agent startup timed out")
            yield client, process
    finally:
        if process.poll() is None:
            process.terminate()
        try:
            process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.communicate()


def run_demo(config, provider, models, environ=None):
    with running_agent(config, environ) as client:
        inventory = client.get("/providers")
        inventory.raise_for_status()
        response = client.post(
            "/scans",
            json={
                "provider_id": provider,
                "endpoint_id": "all",
                "models": models,
                "profile": "chat-basic",
                "max_tokens": 256,
                "max_requests": 8,
                "scan_timeout": 45,
            },
        )
        response.raise_for_status()
        scan_id = response.json()["scan_id"]
        deadline = time.monotonic() + 55
        while time.monotonic() < deadline:
            response = client.get(f"/scans/{scan_id}")
            response.raise_for_status()
            state = response.json()
            if state["state"] in ("completed", "failed", "cancelled"):
                break
            time.sleep(0.05)
        else:
            client.post(f"/scans/{scan_id}/cancel")
            raise RuntimeError("Demo task exceeded its deadline")
        report = client.get(f"/scans/{scan_id}/report")
        report.raise_for_status()
        csv_report = client.get(f"/scans/{scan_id}/report?format=csv")
        csv_report.raise_for_status()
        if not csv_report.content.startswith(b"\xef\xbb\xbf"):
            raise RuntimeError("Missing CSV BOM")
        document = report.json()
        print(
            json.dumps(
                {
                    "task": state,
                    "summary": document["summary"],
                    "results": document["results"],
                    "csv_download": "verified",
                },
                ensure_ascii=False,
                indent=2,
            )
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--provider", default="local-mock")
    parser.add_argument("--model", action="append")
    args = parser.parse_args()
    if args.config:
        if not args.model:
            parser.error("Explicit --model required when using an external provider config")
        run_demo(args.config, args.provider, args.model)
    else:
        from mock_server import running_mock

        with running_mock() as base, tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "mock.toml"
            path.write_text(
                f'''[[providers]]
name = "local-mock"
retries = 0
max_requests = 16
[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_MOCK_UPSTREAM_KEY"
[[providers.endpoints]]
id = "anthropic"
protocol = "anthropic_messages"
base_url = "{base}/v1"
api_key_env = "SCOUT_MOCK_UPSTREAM_KEY"
''',
                encoding="utf-8",
            )
            env = {**os.environ, "SCOUT_MOCK_UPSTREAM_KEY": "synthetic-mock-key"}
            run_demo(path, "local-mock", ["mock-ok", "mock-missing"], env)


if __name__ == "__main__":
    main()
