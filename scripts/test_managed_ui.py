"""Native browser smoke for zero-config provider management using synthetic credentials."""

import importlib.util
import json
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
KEY = "synthetic-ui-managed-key-123"
RESPONSES_KEY = "synthetic-responses-ui-key-456"
ANTHROPIC_KEY = "synthetic-anthropic-ui-key-789"


def module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def connect(page, client):
    page.goto(str(client.base_url), wait_until="networkidle")
    page.locator("#agent-token").fill(client.headers["Authorization"].removeprefix("Bearer "))
    page.locator("#connect").click()
    page.locator("#workspace").wait_for(state="visible")


def check_responses_scan(page, upstream, expect):
    page.locator("#endpoint").select_option("all")
    page.locator("#model-mode").select_option("manual")
    page.locator("#models").fill("mock-ok")
    preview = page.locator("#scan-preview")
    expect(preview).to_contain_text("OpenAI Responses")
    expect(preview).to_contain_text("验证目标 3 项")
    start = len(upstream)
    page.locator("#create-scan").click()
    expect(page.locator("#progress-text")).to_contain_text("已验证 3/3")
    rows = page.locator("#result-rows tr")
    expect(rows).to_have_count(3)
    expect(rows.filter(has_text="mock-ok")).to_have_count(3)
    expect(rows.filter(has_text="OpenAI Chat")).to_have_count(1)
    expect(rows.filter(has_text="Anthropic Messages")).to_have_count(1)
    page.locator("#filter-protocol").select_option("openai_responses")
    expect(rows).to_have_count(1)
    expect(rows).to_contain_text("OpenAI Responses")
    assert sorted(path for path, _, _ in upstream[start:]) == [
        "/v1/chat/completions",
        "/v1/messages",
        "/v1/responses",
    ]

    cases = {
        "mock-responses-refusal": "模型拒绝",
        "mock-responses-empty": "没有可验证的文本回复",
        "mock-responses-incomplete": "响应未完成",
        "mock-responses-error": "响应错误",
    }
    page.locator("#endpoint").select_option("responses")
    page.locator("#models").fill("\n".join(cases))
    page.locator("#max-tokens").fill("96")
    expect(preview).to_contain_text("验证目标 4 项")
    expect(preview).to_contain_text("Token 上限 96")
    start = len(upstream)
    page.locator("#create-scan").click()
    expect(page.locator("#progress-text")).to_contain_text("已验证 4/4")
    for model, diagnostic in cases.items():
        row = rows.filter(has_text=model)
        expect(row).to_contain_text("BAD_RESPONSE")
        expect(row).to_contain_text(diagnostic)
        expect(row.get_by_role("cell").nth(2)).to_have_text("200")
        expect(row.get_by_role("cell").nth(5)).to_have_text("1")
    actual = upstream[start:]
    assert len(actual) == 4
    assert {body["model"] for _, _, body in actual} == set(cases)
    for path, authorization, body in actual:
        assert (path, authorization) == ("/v1/responses", f"Bearer {RESPONSES_KEY}")
        assert body == {
            "model": body["model"],
            "input": "ping",
            "max_output_tokens": 96,
            "stream": False,
            "store": False,
        }
    return page.locator("#selection-title").inner_text().rsplit(" / ", 1)[-1]


def main():
    from playwright.sync_api import expect, sync_playwright

    mock = module("mock_server")
    demo = module("agent_demo")
    upstream = []

    class RecordingHandler(mock.MockHandler):
        def handle_model(self, body):
            upstream.append((urlsplit(self.path).path, self.headers.get("Authorization"), body))
            super().handle_model(body)

    with (
        tempfile.TemporaryDirectory() as temporary,
        mock.running_mock(handler=RecordingHandler) as proxy,
    ):
        data_dir = Path(temporary) / "saved data"
        with demo.running_agent(None, extra_args=["--data-dir", str(data_dir)]) as client:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page(accept_downloads=True)
                    connect(page, client)
                    page.locator("#new-provider").click()
                    page.locator("#provider-name").fill("无配置测试服务商")
                    page.locator("#url-openai").fill("http://upstream.invalid/v1")
                    page.locator("#key-openai").fill(KEY)
                    page.locator("#proxy-openai").fill(
                        proxy.replace("http://", "http://proxy-user:proxy-pass@")
                    )
                    for endpoint, api_key in (
                        ("responses", RESPONSES_KEY),
                        ("anthropic", ANTHROPIC_KEY),
                    ):
                        page.locator(f"#enable-{endpoint}").check()
                        page.locator(f"#url-{endpoint}").fill(f"{proxy}/v1")
                        page.locator(f"#key-{endpoint}").fill(api_key)
                    page.locator("#save-provider").click()
                    expect(page.locator("#provider-dialog")).not_to_be_visible()
                    expect(page.locator("#provider option")).to_have_count(1)
                    assert page.locator("#key-openai").input_value() == ""
                    page.locator("#endpoint").select_option("all")
                    page.locator("#discover-models").click()
                    page.locator("#catalog-panel").wait_for(state="visible")
                    expect(page.locator("#catalog-list input")).to_have_count(6)
                    page.locator("#catalog-protocol").select_option("openai_responses")
                    expect(page.locator("#catalog-list input")).to_have_count(2)
                    expect(page.locator("#catalog-list label").first).to_contain_text(
                        "OpenAI Responses"
                    )
                    page.locator("#catalog-protocol").select_option("openai_chat")
                    page.locator("#catalog-all").click()
                    page.locator("#catalog-test").click()
                    expect(page.locator("#result-rows tr")).to_have_count(2)
                    assert "通过" in page.locator("#result-rows").inner_text()
                    page.locator("#export-provider").click()
                    with page.expect_download() as download:
                        page.locator("#confirm-export").click()
                    public = Path(download.value.path()).read_text(encoding="utf-8")
                    assert all(
                        text not in public
                        for text in (KEY, RESPONSES_KEY, ANTHROPIC_KEY, "proxy-pass")
                    )
                    page.locator("#export-provider").click()
                    page.locator("#export-secrets").check()
                    with page.expect_download() as download:
                        page.locator("#confirm-export").click()
                    secret = json.loads(Path(download.value.path()).read_text(encoding="utf-8"))
                    by_protocol = {
                        item["protocol"]: item for item in secret["provider"]["endpoints"]
                    }
                    assert by_protocol["openai_chat"]["api_key"] == KEY
                    assert "proxy-pass" in by_protocol["openai_chat"]["proxy"]
                    assert by_protocol["openai_responses"]["api_key"] == RESPONSES_KEY
                    page.locator("#edit-provider").click()
                    expect(page.locator("#enable-responses")).to_be_checked()
                    expect(page.locator("#key-responses")).to_have_value("")
                    page.locator("#save-provider").click()
                    expect(page.locator("#provider-dialog")).not_to_be_visible()
                    scan_id = check_responses_scan(page, upstream, expect)
                    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
                    assert KEY not in page.locator("body").inner_text()
                finally:
                    browser.close()
            saved = client.get("/providers").json()["providers"]
            assert len(saved) == 1
            assert all(key not in json.dumps(saved) for key in (KEY, RESPONSES_KEY, ANTHROPIC_KEY))
            db = (data_dir / "providers.sqlite3").read_bytes()
            assert all(key.encode() not in db for key in (KEY, RESPONSES_KEY, ANTHROPIC_KEY))
        with demo.running_agent(None, extra_args=["--data-dir", str(data_dir)]) as restarted:
            restored = restarted.get("/providers").json()["providers"]
            assert len(restored) == 1
            assert restored[0]["provider_id"] == saved[0]["provider_id"]
            assert restored[0]["name"] == "无配置测试服务商"
            assert {e["protocol"] for e in restored[0]["endpoints"]} == {
                "openai_chat",
                "anthropic_messages",
                "openai_responses",
            }
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    connect(page, restarted)
                    prior = len(upstream)
                    page.locator(f'#task-list button[data-job-id="{scan_id}"]').click()
                    expect(page.locator("#result-rows tr")).to_have_count(4)
                    expect(page.locator("#result-rows")).to_contain_text("OpenAI Responses")
                    assert len(upstream) == prior
                    page.locator("#scan-mode").select_option("quick")
                    page.locator("#endpoint").select_option("responses")
                    page.locator("#quick-model").fill("mock-ok")
                    page.locator("#create-scan").click()
                    expect(page.locator("#progress-text")).to_contain_text("已验证 1/1")
                    expect(page.locator("#result-rows tr")).to_contain_text("通过")
                    assert len(upstream) == prior + 1
                    assert upstream[-1][:2] == ("/v1/responses", f"Bearer {RESPONSES_KEY}")
                finally:
                    browser.close()
    print(
        "Managed UI passed: encrypted save/edit/restart, proxy discovery, selected scan, "
        "redacted/explicit-secret export, three protocol lanes and Responses diagnostics."
    )


if __name__ == "__main__":
    main()
