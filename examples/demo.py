"""Run all milestones against a temporary local mock, with no external API calls."""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mock_server import running_mock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--format", choices=("json", "csv"), default="json")
    args = parser.parse_args()
    if args.format == "csv" and args.out is None:
        parser.error("--format csv requires --out")
    env = {k: v for k, v in os.environ.items() if not k.startswith("SCOUT_")}
    env["SCOUT_DEMO_KEY"] = "synthetic-demo-key-only"
    with running_mock() as base, tempfile.TemporaryDirectory() as temp:
        config = Path(temp) / "mock.toml"
        config.write_text(
            f'''[[providers]]
name = "local-mock"
[[providers.endpoints]]
id = "openai"
protocol = "openai_chat"
base_url = "{base}/v1"
api_key_env = "SCOUT_DEMO_KEY"
[[providers.endpoints]]
id = "anthropic"
protocol = "anthropic_messages"
base_url = "{base}/v1"
api_key_env = "SCOUT_DEMO_KEY"
''',
            encoding="utf-8",
        )
        commands = [
            ["models", "--endpoint", "openai"],
            ["verify", "--endpoint", "anthropic", "--model", "mock-ok"],
            ["scan", "--endpoint", "all"],
        ]
        for command in commands:
            invocation = [
                sys.executable,
                "-m",
                "llm_model_scout",
                *command,
                "--config",
                str(config),
            ]
            if command[0] == "scan" and args.out:
                invocation.extend(["--out", str(args.out), "--format", args.format])
            result = subprocess.run(
                invocation, env=env, capture_output=True, text=True, encoding="utf-8", timeout=30
            )
            if result.returncode:
                print(result.stderr, file=sys.stderr)
                return result.returncode
            report = json.loads(result.stdout)
            print(
                f"MOCK {command[0]}: {report['scan']['status']}; "
                f"requests={report['scan']['requests_sent']}; summary={report['summary']}"
            )
        print("Only synthetic localhost results. No real model availability was tested.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
