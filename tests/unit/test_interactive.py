import json
import os
from types import SimpleNamespace

import pytest
from typer.testing import CliRunner

from llm_model_scout.cli import app
from llm_model_scout.domain import ModelResult, ScanReport
from llm_model_scout.interactive import prompt_definition, write_configuration
from llm_model_scout.providers import ProviderStore


def test_edit_prompt_preserves_key_proxy_and_protocol(monkeypatch):
    previous = {
        "name": "old",
        "endpoints": [
            {
                "protocol": "anthropic_messages",
                "base_url": "https://example.test/v1",
                "api_key": "synthetic-key",
                "proxy": "http://u:p@127.0.0.1:8080",
            }
        ],
    }
    answers = iter(["new", "2", "https://example.test/v1", "", ""])
    prompts = []

    def answer(message, **options):
        prompts.append(options)
        return next(answers)

    monkeypatch.setattr("llm_model_scout.interactive.typer.prompt", answer)
    updated = prompt_definition(previous)
    assert prompts[1]["default"] == "2"
    assert updated["endpoints"][0]["api_key"] == "synthetic-key"
    assert updated["endpoints"][0]["proxy"] == previous["endpoints"][0]["proxy"]
    assert prompts[0]["show_default"] is False


def test_edit_default_retains_chat_and_responses_as_separate_connections(monkeypatch):
    previous = {
        "name": "test",
        "endpoints": [
            {
                "protocol": protocol,
                "base_url": "https://example.test/v1",
                "api_key": key,
                "proxy": None,
            }
            for protocol, key in (
                ("openai_chat", "synthetic-chat-key"),
                ("openai_responses", "synthetic-responses-key"),
            )
        ],
    }
    choices = []

    def answer(message, **options):
        if message.startswith("协议"):
            choices.append(options["default"])
        return options["default"]

    monkeypatch.setattr("llm_model_scout.interactive.typer.prompt", answer)
    assert prompt_definition(previous) == previous
    assert choices == ["5"]


def test_configuration_export_is_explicit_and_does_not_overwrite(tmp_path):
    definition = {
        "name": "test",
        "endpoints": [
            {
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "api_key": "synthetic-key",
            }
        ],
    }
    public = tmp_path / "public.json"
    private = tmp_path / "private.json"
    write_configuration(definition, public)
    assert "synthetic-key" not in public.read_text()
    write_configuration(definition, private, include_secrets=True)
    assert json.loads(private.read_text())["provider"]["endpoints"][0]["api_key"] == "synthetic-key"
    before = private.read_bytes()
    with pytest.raises(FileExistsError):
        write_configuration(definition, private, include_secrets=True)
    assert private.read_bytes() == before


def test_interactive_finishes_with_the_scan_plan_and_separate_cancel_count(monkeypatch):
    definition = {
        "name": "test",
        "endpoints": [
            {
                "protocol": "openai_chat",
                "base_url": "https://openai.test/v1",
                "api_key": "synthetic-key",
            },
            {
                "protocol": "anthropic_messages",
                "base_url": "https://anthropic.test/v1",
                "api_key": "synthetic-key",
            },
        ],
    }
    store = SimpleNamespace(records=lambda: {"test": {"definition": definition}})
    monkeypatch.setattr("llm_model_scout.interactive.ProviderStore", lambda _: store)

    async def cancelled_scan(provider, *, on_event):
        on_event({"event": "discovered", "endpoint_id": "openai", "count": 2})
        on_event({"event": "discovered", "endpoint_id": "anthropic", "count": 3})
        on_event({"event": "planned", "count": 3})
        on_event({"event": "result", "endpoint_id": "openai", "model": "a", "status": "available"})
        return ScanReport(
            provider.name,
            [],
            {},
            status="cancelled",
            results=[
                ModelResult("openai", "openai_chat", "a", "chat-basic", status="available"),
                ModelResult("openai", "openai_chat", "b", "chat-basic", status="cancelled"),
                ModelResult("anthropic", "anthropic_messages", "c", "chat-basic"),
            ],
        )

    monkeypatch.setattr("llm_model_scout.interactive.scan", cancelled_scan)
    result = CliRunner().invoke(app, ["interactive"], input="5\n0\n")
    assert result.exit_code == 0, result.output
    assert "（累计 5）" in result.output
    assert "扫描已取消 · 已验证 1/3（33.3%） · 跳过 1 项 · 取消 1 项" in result.output


@pytest.mark.skipif(os.name != "nt", reason="Windows DPAPI-backed interactive flow")
def test_interactive_hidden_key_and_automatic_save(tmp_path):
    result = CliRunner().invoke(
        app,
        ["interactive", "--data-dir", str(tmp_path)],
        input="1\nCLI provider\n1\nhttps://example.test/v1\nsynthetic-cli-secret\n\n0\n",
    )
    assert result.exit_code == 0, result.output
    assert "synthetic-cli-secret" not in result.output
    records = ProviderStore(tmp_path / "providers.sqlite3").records()
    assert len(records) == 1
    assert (
        next(iter(records.values()))["definition"]["endpoints"][0]["api_key"]
        == "synthetic-cli-secret"
    )


def test_interactive_tool_actions_preserve_discovery_and_normalize_manual_ids(monkeypatch):
    definition = {
        "name": "Example",
        "endpoints": [
            {
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "api_key": "synthetic-key",
            }
        ],
    }
    store = SimpleNamespace(records=lambda: {"saved": {"definition": definition}})
    monkeypatch.setattr("llm_model_scout.interactive.ProviderStore", lambda _: store)
    selections = []

    async def scan(provider, **kwargs):
        (endpoint,) = provider.endpoints
        selections.append((endpoint.probe.capability, endpoint.models))
        return ScanReport(provider.name, [], {})

    monkeypatch.setattr("llm_model_scout.interactive.scan", scan)
    result = CliRunner().invoke(app, ["interactive"], input="14\n15\nm, m, M\n0\n")
    assert result.exit_code == 0, result.output
    assert selections == [("tool", None), ("tool", ("m", "M"))]
