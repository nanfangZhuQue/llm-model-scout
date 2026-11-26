import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("check_docs", ROOT / "scripts/check_docs.py")
checker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(checker)


def test_repository_documentation_links():
    assert checker.check_docs(ROOT) == []


def test_link_checker_reports_missing_and_escaping_targets(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text("[missing](missing.md)\n[outside](../outside.md)\n", encoding="utf-8")
    errors = checker.check_links(tmp_path, [doc])
    assert len(errors) == 2
    assert "missing link target" in errors[0]
    assert "escapes project" in errors[1]


def test_link_checker_ignores_examples_and_external_urls(tmp_path):
    doc = tmp_path / "doc.md"
    doc.write_text(
        "```text\n[example](not-real.md)\n```\n[external](https://example.test)\n[anchor](#x)\n",
        encoding="utf-8",
    )
    assert checker.check_links(tmp_path, [doc]) == []


def test_prompt_hook_does_not_echo_credentials():
    response = checker.hook_response(
        {"hook_event_name": "UserPromptSubmit", "prompt": "synthetic-secret-must-not-echo"}
    )
    assert "synthetic-secret-must-not-echo" not in str(response)
    assert response["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_stop_hook_blocks_once_on_missing_docs(tmp_path):
    response = checker.hook_response({"hook_event_name": "Stop"}, tmp_path)
    assert response["decision"] == "block"
    again = checker.hook_response({"hook_event_name": "Stop", "stop_hook_active": True}, tmp_path)
    assert "decision" not in again
    assert "systemMessage" in again


def test_stop_hook_clean_project_does_not_block():
    assert checker.hook_response({"hook_event_name": "Stop"}, ROOT) == {}
