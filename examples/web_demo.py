"""Serve the same-origin UI with synthetic upstreams; no external API calls."""

import argparse
import json

import uvicorn
from mock_server import running_mock

from llm_model_scout.agent.app import create_app
from llm_model_scout.agent.config import prepare_token, remove_owned_token
from llm_model_scout.domain import EndpointConfig, ProbeConfig, ProviderConfig, ScanPolicy


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--token-env")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1-65535")
    token, owned = prepare_token(args.token_env, None)
    try:
        with running_mock() as base:
            endpoints = tuple(
                EndpointConfig(
                    name,
                    protocol,
                    base + "/v1",
                    "synthetic-mock-key",
                    probe=ProbeConfig.named("chat-basic"),
                )
                for name, protocol in (
                    ("openai", "openai_chat"),
                    ("anthropic", "anthropic_messages"),
                )
            )
            provider = ProviderConfig(
                "local-mock",
                endpoints,
                ScanPolicy(concurrency=2, retries=0, max_requests=200, scan_timeout=30),
            )
            app = create_app({"local-mock": provider}, token=token, port=args.port)
            print(
                json.dumps(
                    {
                        "demo": "synthetic localhost only",
                        "url": f"http://127.0.0.1:{args.port}",
                        "token_file": str(owned) if owned else None,
                    }
                ),
                flush=True,
            )
            uvicorn.run(
                app,
                host="127.0.0.1",
                port=args.port,
                access_log=False,
                proxy_headers=False,
                log_level="warning",
            )
    finally:
        remove_owned_token(owned, token)


if __name__ == "__main__":
    main()
