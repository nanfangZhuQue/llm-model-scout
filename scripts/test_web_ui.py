"""Optional native Playwright UI regression. Requires .[browser] and Chromium."""

import argparse
import importlib.util
import json
import os
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import load_workbook

ROOT = Path(__file__).resolve().parents[1]


def example_module(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "examples" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def download_workbook(page, button_name, filename_suffix):
    with page.expect_download() as downloading:
        page.get_by_role("button", name=button_name, exact=True).click()
    download = downloading.value
    assert download.suggested_filename.endswith(filename_suffix)
    with Path(download.path()).open("rb") as file:
        sheet = load_workbook(file).active
    assert sheet.auto_filter.ref == sheet.dimensions
    header, *rows = sheet.values
    model_col = header.index("模型")
    status_col = header.index("模型状态")
    return sorted((row[model_col], row[status_col]) for row in rows)


def exercise(page, url, token, screenshots):
    from playwright.sync_api import expect

    errors = []
    page.on("pageerror", lambda error: errors.append(str(error)))
    page.goto(url, wait_until="networkidle")
    page.get_by_label("Agent 会话 Token", exact=True).fill("wrong-token-" + "x" * 32)
    page.get_by_role("button", name="连接工作台", exact=True).click()
    expect(page.locator("#notice")).to_contain_text("无效")
    page.get_by_label("Agent 会话 Token", exact=True).fill(token)
    page.get_by_role("button", name="连接工作台", exact=True).click()
    page.locator("#workspace").wait_for(state="visible")
    page.locator("#endpoint").select_option("all")
    page.locator("#model-mode").select_option("manual")
    markup_model = "<img src=x onerror=alert(1)>"
    page.locator("#models").fill(f"mock-ok\nmock-missing\n{markup_model}")
    page.get_by_role("button", name="创建扫描任务", exact=True).click()
    expect(page.locator("#result-rows tr")).to_have_count(6)
    assert page.locator("#result-rows img").count() == 0
    page.locator("#filter-status").select_option("unavailable")
    assert page.locator("#result-rows tr").count() == 2
    expected_passed = [(markup_model, "通过"), ("mock-ok", "通过")] * 2
    expected_failed = [("mock-missing", "未通过")] * 2
    all_rows = download_workbook(page, "下载 XLSX（全部）", ".xlsx")
    assert all_rows == sorted(expected_passed + expected_failed)
    passed_rows = download_workbook(page, "下载 XLSX（仅通过）", "-仅通过.xlsx")
    assert passed_rows == sorted(expected_passed)
    page.locator("#filter-status").select_option("")
    assert page.evaluate("localStorage.length === 0 && sessionStorage.length === 0")
    assert page.locator("#agent-token").input_value() == ""
    page.set_viewport_size({"width": 1360, "height": 1000})
    page.screenshot(path=str(screenshots / "ui-desktop.png"), full_page=True)
    page.get_by_role("button", name="切换主题", exact=True).click()
    page.set_viewport_size({"width": 390, "height": 844})
    assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
    page.screenshot(path=str(screenshots / "ui-mobile-dark.png"), full_page=True)
    page.reload(wait_until="networkidle")
    assert page.locator("#login-panel").is_visible()
    page.get_by_label("Agent 会话 Token", exact=True).fill(token)
    page.get_by_role("button", name="连接工作台", exact=True).click()
    page.locator("[data-job-id]").first.wait_for(state="visible")
    page.locator("#model-mode").select_option("manual")
    page.locator("#models").fill("mock-slow")
    page.get_by_role("button", name="创建扫描任务", exact=True).click()
    expect(page.locator("#cancel-scan")).to_be_enabled()
    page.get_by_role("button", name="取消扫描", exact=True).click()
    expect(page.locator("#selection-state")).to_contain_text("已取消")
    page.get_by_role("button", name="退出连接", exact=True).click()
    assert page.locator("#login-panel").is_visible()
    assert not errors, errors


def exercise_progress(page, url, token):
    from playwright.sync_api import expect

    page.goto(url, wait_until="networkidle")
    page.get_by_label("Agent 会话 Token", exact=True).fill(token)
    page.get_by_role("button", name="连接工作台", exact=True).click()
    expect(page.locator("#workspace")).to_be_visible()
    page.locator("#endpoint").select_option("all")
    page.locator("#discover-models").click()
    expect(page.locator("#catalog-list input")).to_have_count(5)
    expect(page.locator("#selection-progress")).to_have_text("已发现 5 项")
    expect(page.locator("#progress-text")).to_have_text("模型发现完成")
    expect(page.locator("#all-preview")).to_contain_text("验证目标 待发现")
    progress = page.get_by_role("progressbar", name="验证进度")
    expect(progress).to_be_hidden()

    def submit_models(*names):
        page.locator("#catalog-none").click()
        for name in names:
            page.get_by_role("checkbox", name=name, exact=True).check()
        expect(page.locator("#selected-preview")).to_contain_text(f"验证目标 {len(names)} 项")
        page.locator("#catalog-test").click()

    fast_models = (
        "openai · OpenAI Chat / mock-missing",
        "openai · OpenAI Chat / mock-ok",
        "anthropic · Anthropic Messages / mock-ok",
    )
    submit_models(*fast_models)
    expect(page.locator("#progress-text")).to_have_text("已验证 3/3 项（100%）")
    expect(page.locator("#selection-state")).to_contain_text("执行完成")
    expect(progress).to_have_attribute("aria-valuenow", "3")
    expect(progress).to_have_attribute("aria-valuemax", "3")

    submit_models(
        "openai · OpenAI Chat / mock-ok",
        "anthropic · Anthropic Messages / mock-hold-a",
        "anthropic · Anthropic Messages / mock-hold-b",
    )
    expect(page.locator("#progress-text")).to_have_text("已验证 1/3 项（33%）")
    page.locator("#cancel-scan").click()
    expect(page.locator("#selection-state")).to_contain_text("已取消")
    expect(page.locator("#progress-text")).to_have_text("已验证 1/3 项（33%）")
    expect(progress).to_have_attribute("aria-valuenow", "1")
    expect(progress).to_have_attribute("aria-valuemax", "3")
    expect(page.locator("#selection-progress")).to_have_text(
        "已发现 3 项 · 未执行 1 项 · 已取消 1 项"
    )

    page.locator("#max-requests").fill("1")
    submit_models(*fast_models)
    expect(page.locator("#selection-state")).to_contain_text("执行失败")
    expect(page.locator("#progress-text")).to_have_text("已验证 1/3 项（33%）")
    expect(page.locator("#selection-progress")).to_have_text("已发现 3 项 · 未执行 2 项")


def seed_history_page(history, scan_id):
    saved = history.get(scan_id)
    created_at = datetime.fromisoformat(saved["view"]["created_at"])
    history.open()
    try:
        for i in range(1, 51):
            stamp = (created_at + timedelta(seconds=i)).isoformat()
            view = {
                **saved["view"],
                "scan_id": uuid.uuid4().hex,
                "state": "queued",
                "created_at": stamp,
                "started_at": None,
                "finished_at": None,
                "progress": {"discovered": 0, "finished": 0},
                "report_available": False,
            }
            history.create(view, saved["conditions"])
            history.finish({**view, "state": "cancelled", "finished_at": stamp}, None)
    finally:
        history.close()


def exercise_history(page, demo, mock_url, folder):
    from playwright.sync_api import expect

    from llm_model_scout.agent.history import HistoryStore

    def provider_config(name):
        return f'''[[providers]]
name="{name}"
retries=0
concurrency=1
[[providers.endpoints]]
id="openai"
protocol="openai_chat"
base_url="{mock_url}/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
'''

    config = folder / "history.toml"
    config.write_text(provider_config("current") + provider_config("retired"), encoding="utf-8")
    data_dir = folder / "history-data"
    env = {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"}
    args = ["--data-dir", str(data_dir), "--max-history", "60"]

    def run_scan(client, provider_id, models, *, interrupt=False):
        response = client.post("/scans", json={"provider_id": provider_id, "models": models})
        response.raise_for_status()
        scan_id = response.json()["scan_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail = client.get(f"/scans/{scan_id}")
            detail.raise_for_status()
            job = detail.json()
            if interrupt:
                if job["state"] == "running" and job["progress"]["finished"] == 1:
                    return scan_id
            elif job["state"] == "completed":
                return scan_id
            time.sleep(0.02)
        raise AssertionError("History fixture did not reach its expected scan state")

    with demo.running_agent_process(config, env, args) as (client, process):
        run_scan(client, "current", ["mock-ok"])
        full_id = run_scan(client, "retired", ["mock-ok", "mock-missing"])
        full_report = client.get(f"/scans/{full_id}/report").json()
        partial_id = run_scan(client, "retired", ["mock-ok", "mock-z-hold"], interrupt=True)
        process.kill()
        process.wait(timeout=10)
    seed_history_page(HistoryStore(data_dir / "scans.sqlite3"), partial_id)

    config.write_text(provider_config("current"), encoding="utf-8")
    with demo.running_agent(config, env, args) as client:
        token = client.headers["Authorization"].removeprefix("Bearer ")
        page.goto(str(client.base_url), wait_until="networkidle")
        page.get_by_label("Agent 会话 Token", exact=True).fill(token)
        page.get_by_role("button", name="连接工作台", exact=True).click()
        filter_provider = page.get_by_label("按服务商筛选任务")
        expect(filter_provider.locator("option")).to_have_count(3)
        expect(page.locator("#task-count")).to_have_text("53 个保留任务")
        expect(page.locator("#provider")).to_have_value("current")

        filter_provider.select_option("retired")
        expect(page.locator("#task-count")).to_have_text("52 个保留任务")
        page.get_by_role("button", name="下一页", exact=True).click()
        expect(page.locator("#page-info")).to_have_text("第 2 页")
        expect(page.locator("[data-job-id]")).to_have_count(2)
        with page.expect_response(lambda response: "offset=50&provider_id=retired" in response.url):
            page.locator("#refresh").click()
        expect(filter_provider).to_have_value("retired")
        expect(page.locator("#page-info")).to_have_text("第 2 页")
        expect(page.locator("#provider")).to_have_value("current")

        page.locator(f'[data-job-id="{full_id}"]').click()
        expect(page.locator("#selection-partial")).to_be_hidden()
        expect(page.locator("#result-rows tr")).to_have_count(2)
        json_button = page.get_by_role("button", name="下载 JSON", exact=True)
        with page.expect_download() as complete:
            json_button.click()
        assert complete.value.suggested_filename == f"scan-{full_id}.json"
        assert json.loads(Path(complete.value.path()).read_text(encoding="utf-8")) == full_report

        page.locator(f'[data-job-id="{partial_id}"]').click()
        expect(page.locator("#selection-state")).to_contain_text("执行中断")
        expect(page.locator("#selection-partial")).to_be_visible()
        expect(page.locator("#result-rows tr")).to_have_count(1)
        expect(page.locator("#cancel-scan")).to_be_disabled()
        with page.expect_download() as partial:
            json_button.click()
        assert partial.value.suggested_filename == f"scan-{partial_id}-partial.json"
        report = json.loads(Path(partial.value.path()).read_text(encoding="utf-8"))
        assert report["scan"]["partial"] is True
        assert [(row["model"], row["status"]) for row in report["results"]] == [
            ("mock-ok", "available")
        ]
        assert download_workbook(page, "下载 XLSX（全部）", "-部分.xlsx") == [("mock-ok", "通过")]
        page.get_by_role("button", name="设为基准", exact=True).click()
        filter_provider.select_option("current")
        expect(page.locator("#task-count")).to_have_text("1 个保留任务")
        expect(page.locator("#page-info")).to_have_text("第 1 页")
        page.locator("[data-job-id]").click()
        page.get_by_role("button", name="设为本次", exact=True).click()
        expect(page.locator("#compare-message")).to_contain_text("请选择同一服务商且有共同协议入口")


def exercise_comparison(page, demo, mock_url, folder, catalog_changed):
    from playwright.sync_api import expect

    config = folder / "comparison.toml"
    config.write_text(
        f"""[[providers]]
name="compare-mock"
retries=0
[[providers.endpoints]]
id="openai"
protocol="openai_chat"
base_url="{mock_url}/comparison/openai/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
[[providers.endpoints]]
id="anthropic"
protocol="anthropic_messages"
base_url="{mock_url}/comparison/anthropic/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
""",
        encoding="utf-8",
    )
    env = {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"}
    args = ["--data-dir", str(folder / "comparison-data")]

    def scan(client, **selection):
        response = client.post("/scans", json={"provider_id": "compare-mock", **selection})
        response.raise_for_status()
        scan_id = response.json()["scan_id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail = client.get(f"/scans/{scan_id}")
            detail.raise_for_status()
            if detail.json()["state"] == "completed":
                return scan_id
            time.sleep(0.02)
        raise AssertionError("Comparison fixture did not complete")

    with demo.running_agent(config, env, args) as client:
        baseline = scan(client, endpoint_id="all", max_tokens=48)
        catalog_changed.set()
        current = scan(client, endpoint_id="all", max_tokens=48)
        changed = scan(client, endpoint_id="openai", models=["mock-stable"], max_tokens=64)

    config.write_text(
        config.read_text(encoding="utf-8").replace(
            "/comparison/openai/v1", "/comparison/openai/live/v1"
        ),
        encoding="utf-8",
    )
    with demo.running_agent(config, env, args) as client:
        token = client.headers["Authorization"].removeprefix("Bearer ")
        page.goto(str(client.base_url), wait_until="networkidle")
        page.get_by_label("Agent 会话 Token", exact=True).fill(token)
        page.get_by_role("button", name="连接工作台", exact=True).click()

        def select(scan_id, action):
            page.locator(f'[data-job-id="{scan_id}"]').click()
            page.get_by_role("button", name=action, exact=True).click()

        def comparison_row(model):
            return page.locator("#compare-rows tr").filter(
                has=page.get_by_role("cell", name=model, exact=True)
            )

        select(baseline, "设为基准")
        expect(page.locator("#compare-baseline-label")).to_contain_text(baseline[:8])
        select(current, "设为本次")
        expect(page.locator("#compare-current-label")).to_contain_text(current[:8])
        expect(page.locator("#compare-message")).to_have_text("请选择要对比的共同入口。")
        endpoint = page.get_by_label("对比协议入口")
        endpoint.select_option("openai")
        expect(page.locator("#compare-credentials")).to_have_text("凭据变化未知")
        expect(comparison_row("mock-regression")).to_contain_text("通过转为未通过")
        expect(comparison_row("mock-recovery")).to_contain_text("未通过转为通过")
        expect(comparison_row("mock-stable")).to_contain_text("结果不变")
        expect(comparison_row("mock-removed")).to_contain_text("本次未测")
        expect(comparison_row("mock-removed")).to_contain_text("目录移除")
        expect(comparison_row("mock-added")).to_contain_text("基准未测")
        expect(comparison_row("mock-added")).to_contain_text("目录新增")
        page.set_viewport_size({"width": 390, "height": 844})
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        page.get_by_label("对比结果筛选").select_option("catalog")
        expect(page.locator("#compare-rows tr")).to_have_count(2)
        comparison_filter = page.get_by_label("对比结果筛选")
        comparison_filter.select_option("regressed")
        page.get_by_role("checkbox", name="复测 mock-regression", exact=True).check()
        comparison_filter.select_option("recovered")
        page.get_by_role("checkbox", name="复测 mock-recovery", exact=True).check()
        expect(page.locator("#retest-count")).to_have_text("已选 2 项")
        comparison_filter.select_option("")
        expect(
            page.get_by_role("checkbox", name="复测 mock-regression", exact=True)
        ).to_be_checked()
        expect(page.get_by_role("checkbox", name="复测 mock-stable", exact=True)).to_have_count(0)
        count = client.get("/scans").json()["total"]

        def retest_response(response):
            return response.url.endswith("/scans/retest") and response.request.method == "POST"

        with page.expect_response(retest_response) as preview:
            page.get_by_role("button", name="复测所选", exact=True).click()
        assert preview.value.status == 409
        expect(page.locator("#retest-connection-changes")).to_contain_text("入口地址")
        assert client.get("/scans").json()["total"] == count
        confirm = page.get_by_role("button", name="确认使用当前连接复测", exact=True)
        with page.expect_response(retest_response) as submitted:
            confirm.evaluate("button => { button.click(); button.click(); }")
        assert submitted.value.status == 202
        assert (
            submitted.value.request.post_data_json["request_id"]
            == preview.value.request.post_data_json["request_id"]
        )
        assert set(submitted.value.request.post_data_json["models"]) == {
            "mock-regression",
            "mock-recovery",
        }
        retest = submitted.value.json()
        assert retest["source"] == {
            "baseline_id": baseline,
            "current_id": current,
            "endpoint_id": "openai",
        }
        expect(page.locator("#selection-title")).to_contain_text(retest["scan_id"])
        expect(page.locator("#selection-source-label")).to_have_text("来源对比 · openai")
        expect(page.locator("#result-rows tr")).to_have_count(2)
        report = client.get(f"/scans/{retest['scan_id']}/report").json()
        assert report["scan"]["requests_sent"] == 2
        assert report["discovery"][0]["source"] == "manual"
        assert report["provider"]["endpoints"][0]["probe"]["max_tokens"] == 48
        assert {row["model"] for row in report["results"]} == {"mock-regression", "mock-recovery"}
        assert client.get("/scans").json()["total"] == count + 1
        page.get_by_role("button", name="查看基准任务", exact=True).click()
        expect(page.locator("#selection-title")).to_contain_text(baseline)

        endpoint.select_option("anthropic")
        expect(page.locator("#compare-rows tr")).to_have_count(1)
        expect(comparison_row("mock-regression")).to_contain_text("结果不变")

        select(changed, "设为本次")
        expect(endpoint).to_have_value("openai")
        expect(page.locator("#compare-conditions")).to_have_text("探测条件变化")
        expect(page.locator("#compare-condition-changes")).to_contain_text("Token 上限")
        expect(page.locator("#compare-catalog-status")).to_have_text("目录新增与移除无法判定")
        expect(comparison_row("mock-stable")).to_contain_text("探测条件变化")
        expect(comparison_row("mock-regression")).to_contain_text("本次未测")
        page.get_by_role("button", name="清除对比", exact=True).click()
        expect(page.locator("#compare-panel")).to_be_hidden()


def exercise_diagnostics(page, demo, mock_url, folder, calls):
    from playwright.sync_api import expect

    config = folder / "diagnostic.toml"
    config.write_text(
        f'''[[providers]]
name="diagnostic"
retries=2
timeout=0.3
max_requests=12
[[providers.endpoints]]
id="openai"
protocol="openai_chat"
base_url="{mock_url}/diagnostic/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
''',
        encoding="utf-8",
    )
    env = {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"}
    args = ["--data-dir", str(folder / "diagnostic-data")]
    with demo.running_agent(config, env, args) as client:
        page.goto(str(client.base_url), wait_until="networkidle")
        page.get_by_label("Agent 会话 Token", exact=True).fill(
            client.headers["Authorization"].removeprefix("Bearer ")
        )
        page.get_by_role("button", name="连接工作台", exact=True).click()
        preview = page.locator("#scan-preview")
        expect(preview).to_contain_text("验证目标 待发现")
        expect(preview).to_contain_text("OpenAI Chat")
        expect(preview).to_contain_text("任务预算 12 次")
        page.locator("#model-mode").select_option("manual")
        page.locator("#models").fill("mock-auth\nmock-rate")
        expect(preview).to_contain_text("验证目标 2 项")
        expect(preview).to_contain_text("本次最多 6 次请求")
        assert not calls

        page.get_by_label("验收方式").select_option("quick")
        page.locator("#max-tokens").fill("32")
        create = page.get_by_role("button", name="验证指定模型", exact=True)
        cases = (
            ("mock-auth", "401", "UNAUTHORIZED", "鉴权失败"),
            ("mock-rate", "429", "RATE_LIMITED", "限流"),
            ("mock-timeout", "—", "TIMEOUT", "请求超时"),
            ("mock-empty", "200", "BAD_RESPONSE", "没有可验证的文本"),
        )
        for index, (model, status, category, detail) in enumerate(cases):
            page.get_by_label("指定模型 ID").fill(model)
            expect(preview).to_contain_text("验证目标 1 项")
            expect(preview).to_contain_text("Token 上限 32")
            expect(preview).to_contain_text("任务预算 1 次")
            expect(preview).to_contain_text("本次最多 1 次请求 · 重试 0 次")
            expect(create).to_be_enabled()
            assert len(calls) == index
            create.click()
            row = page.locator("#result-rows tr").filter(has_text=model)
            expect(row.get_by_role("cell").nth(2)).to_have_text(status)
            expect(row.get_by_role("cell").nth(5)).to_have_text("1")
            expect(row).to_contain_text(category)
            expect(row).to_contain_text(detail)
            assert calls[index:] == [("POST", model, 32)]


def exercise_streaming(page, demo, mock_url, folder):
    from playwright.sync_api import expect

    config = folder / "streaming.toml"
    config.write_text(
        f'''[[providers]]
name="streaming"
retries=0
[[providers.endpoints]]
id="chat"
protocol="openai_chat"
base_url="{mock_url}/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
''',
        encoding="utf-8",
    )
    env = {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"}
    with demo.running_agent(config, env, ["--data-dir", str(folder / "streaming-data")]) as client:
        page.goto(str(client.base_url), wait_until="networkidle")
        page.get_by_label("Agent 会话 Token", exact=True).fill(
            client.headers["Authorization"].removeprefix("Bearer ")
        )
        page.get_by_role("button", name="连接工作台", exact=True).click()
        page.get_by_label("验收方式").select_option("quick")
        page.get_by_label("探测能力").select_option("stream")
        page.get_by_label("指定模型 ID").fill("mock-stream-ok")
        preview = page.locator("#scan-preview")
        expect(preview).to_contain_text("流式文本")
        expect(preview).to_contain_text("本次最多 1 次请求")
        page.get_by_role("button", name="验证指定模型", exact=True).click()
        results = page.locator("#result-rows tr")
        success = results.filter(has_text="mock-stream-ok")
        expect(success).to_contain_text("流式文本")
        expect(success.get_by_role("cell").nth(1)).to_have_text("通过")
        expect(page.locator("#selection-capability")).to_contain_text("流式文本")

        page.get_by_label("验收方式").select_option("batch")
        page.locator("#model-mode").select_option("manual")
        page.locator("#models").fill("mock-stream-empty\nmock-stream-abrupt")
        expect(preview).to_contain_text("验证目标 2 项")
        expect(preview).to_contain_text("本次最多 2 次请求")
        page.get_by_role("button", name="创建扫描任务", exact=True).click()
        incomplete = results.filter(has_text="mock-stream-abrupt")
        expect(incomplete).to_contain_text("流式响应中断")
        expect(incomplete.get_by_role("cell").nth(1)).to_have_text("未通过")
        expect(results.filter(has_text="mock-stream-empty")).to_contain_text("空流")
        expect(page.locator("#progress-text")).to_have_text("已验证 2/2 项（100%）")


def exercise_structured(page, demo, mock_url, folder, calls):
    from playwright.sync_api import expect

    config = folder / "structured.toml"
    config.write_text(
        f'''[[providers]]
name="structured"
retries=0
[[providers.endpoints]]
id="chat"
protocol="openai_chat"
base_url="{mock_url}/structured/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
''',
        encoding="utf-8",
    )
    env = {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"}
    with demo.running_agent(config, env, ["--data-dir", str(folder / "structured-data")]) as client:
        page.goto(str(client.base_url), wait_until="networkidle")
        page.get_by_label("Agent 会话 Token", exact=True).fill(
            client.headers["Authorization"].removeprefix("Bearer ")
        )
        page.get_by_role("button", name="连接工作台", exact=True).click()
        page.get_by_label("验收方式").select_option("quick")
        page.get_by_label("探测能力").select_option("structured")
        page.get_by_label("指定模型 ID").fill("mock-structured-ok")
        preview = page.locator("#scan-preview")
        expect(preview).to_contain_text("结构化输出")
        expect(preview).to_contain_text("本次最多 1 次请求")
        assert not calls
        page.get_by_role("button", name="验证指定模型", exact=True).click()
        results = page.locator("#result-rows tr")
        success = results.filter(has_text="mock-structured-ok")
        expect(success).to_contain_text("结构化输出")
        expect(success.get_by_role("cell").nth(1)).to_have_text("通过")
        expect(page.locator("#selection-capability")).to_contain_text("结构化输出")
        assert len(calls) == 1 and calls[0]["response_format"]["type"] == "json_schema"

        page.get_by_label("验收方式").select_option("batch")
        page.locator("#model-mode").select_option("manual")
        page.locator("#models").fill(
            "mock-structured-ok\nmock-structured-wrong-type\nmock-structured-invalid"
        )
        expect(preview).to_contain_text("验证目标 3 项")
        expect(preview).to_contain_text("本次最多 3 次请求")
        assert len(calls) == 1
        page.get_by_role("button", name="创建扫描任务", exact=True).click()
        expect(results.filter(has_text="mock-structured-wrong-type")).to_contain_text(
            "schema_mismatch"
        )
        expect(results.filter(has_text="mock-structured-invalid")).to_contain_text(
            "invalid_structured_json"
        )
        expect(page.locator("#result-rows")).not_to_contain_text(
            "synthetic-private-structured-text"
        )
        expect(page.locator("#progress-text")).to_have_text("已验证 3/3 项（100%）")
        assert download_workbook(page, "下载 XLSX（仅通过）", "-仅通过.xlsx") == [
            ("mock-structured-ok", "通过")
        ]
        assert len(calls) == 4


def exercise_tool_probe(page, demo, mock_url, folder, calls):
    from playwright.sync_api import expect

    config = folder / "tools.toml"
    config.write_text(
        f'''[[providers]]
name="tools"
retries=0
[[providers.endpoints]]
id="chat"
protocol="openai_chat"
base_url="{mock_url}/tool/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
profile="chat-basic"
max_tokens=128
''',
        encoding="utf-8",
    )
    expected = {
        "mock-tool-ok": None,
        "mock-tool-none": "no_tool_call",
        "mock-tool-wrong-name": "unexpected_tool_name",
        "mock-tool-wrong-args": "invalid_tool_arguments",
        "mock-tool-multiple": "unexpected_tool_call_count",
    }
    env = {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"}
    with demo.running_agent(config, env, ["--data-dir", str(folder / "tool-data")]) as client:
        page.goto(str(client.base_url), wait_until="networkidle")
        page.get_by_label("Agent 会话 Token", exact=True).fill(
            client.headers["Authorization"].removeprefix("Bearer ")
        )
        page.get_by_role("button", name="连接工作台", exact=True).click()
        page.get_by_label("验收方式").select_option("quick")
        page.get_by_label("探测能力").select_option("tool")
        page.get_by_label("指定模型 ID").fill("mock-tool-ok")
        preview = page.locator("#scan-preview")
        expect(preview).to_contain_text("工具调用")
        expect(preview).to_contain_text("本次最多 1 次请求")
        assert not calls
        page.get_by_role("button", name="验证指定模型", exact=True).click()
        rows = page.locator("#result-rows tr")
        valid = rows.filter(has_text="mock-tool-ok")
        expect(valid.get_by_role("cell").nth(1)).to_have_text("通过")
        expect(page.locator("#selection-capability")).to_contain_text("工具调用")
        assert len(calls) == 1

        page.get_by_label("验收方式").select_option("batch")
        page.locator("#model-mode").select_option("manual")
        page.locator("#models").fill("\n".join(expected))
        expect(preview).to_contain_text("验证目标 5 项")
        expect(preview).to_contain_text("本次最多 5 次请求")
        assert len(calls) == 1
        page.get_by_role("button", name="创建扫描任务", exact=True).click()
        for model, code in expected.items():
            if code:
                expect(rows.filter(has_text=model)).to_contain_text(code)
        expect(page.locator("#progress-text")).to_have_text("已验证 5/5 项（100%）")
        expect(page.locator("#result-rows")).not_to_contain_text("synthetic-private-tool")
        expect(page.locator("#result-rows")).not_to_contain_text("synthetic_private_")
        assert download_workbook(page, "下载 XLSX（仅通过）", "-仅通过.xlsx") == [
            ("mock-tool-ok", "通过")
        ]
        assert len(calls) == 6
        assert all(
            body["tools"][0]["function"]["name"] == "scout_tool_probe"
            and [message["role"] for message in body["messages"]] == ["user"]
            for body in calls
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshots", type=Path, default=ROOT / ".scout")
    args = parser.parse_args()
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        parser.error(
            'Install: python -m pip install -e ".[browser]"; python -m playwright install chromium'
        )
    args.screenshots.mkdir(parents=True, exist_ok=True)
    mock = example_module("mock_server")
    demo = example_module("agent_demo")
    release = threading.Event()
    history_release = threading.Event()
    catalog_changed = threading.Event()
    diagnostic_calls = []
    structured_calls = []
    tool_calls = []

    class ProgressHandler(mock.MockHandler):
        def do_GET(self):
            if self.path == "/tool/v1/models":
                tool_calls.append(None)
                super().do_GET()
            elif self.path == "/diagnostic/v1/models":
                diagnostic_calls.append(("GET", None, None))
                self.respond(404, {"error": {"message": "Model discovery unavailable"}})
            elif self.path == "/comparison/openai/v1/models":
                models = ["mock-regression", "mock-recovery", "mock-stable"]
                models.append("mock-added" if catalog_changed.is_set() else "mock-removed")
                self.respond(200, {"data": [{"id": model} for model in models]})
            elif self.path == "/comparison/anthropic/v1/models":
                self.respond(200, {"data": [{"id": "mock-regression"}]})
            elif self.path == "/anthropic/v1/models":
                models = ["mock-ok", "mock-hold-a", "mock-hold-b"]
                self.respond(200, {"data": [{"id": model} for model in models]})
            else:
                super().do_GET()

        def do_POST(self):
            if self.path == "/diagnostic/v1/chat/completions":
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                model = body["model"]
                diagnostic_calls.append(("POST", model, body["max_tokens"]))
                status = {"mock-auth": 401, "mock-rate": 429}.get(model)
                if status is not None:
                    self.respond(status, {"error": {"message": "Synthetic error"}})
                else:
                    if model == "mock-timeout":
                        time.sleep(0.8)
                    content = "" if model == "mock-empty" else "MOCK pong"
                    self.respond(
                        200, {"choices": [{"message": {"role": "assistant", "content": content}}]}
                    )
                return
            if self.path not in (
                "/comparison/openai/v1/chat/completions",
                "/comparison/openai/live/v1/chat/completions",
            ):
                super().do_POST()
                return
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            failing_model = "mock-regression" if catalog_changed.is_set() else "mock-recovery"
            if body["model"] == failing_model:
                self.respond(403, {"error": {"message": "Forbidden"}})
            else:
                self.respond(
                    200, {"choices": [{"message": {"role": "assistant", "content": "MOCK pong"}}]}
                )

        def wait_for_model(self, model):
            if model in ("mock-hold-a", "mock-hold-b"):
                release.wait(30)
            elif model == "mock-z-hold":
                history_release.wait(30)
            else:
                super().wait_for_model(model)

        def handle_model(self, body):
            if self.path.startswith("/structured/v1/"):
                structured_calls.append(body)
            elif self.path.startswith("/tool/v1/"):
                tool_calls.append(body)
            super().handle_model(body)

    with (
        mock.running_mock(handler=ProgressHandler) as base,
        tempfile.TemporaryDirectory() as folder,
        sync_playwright() as playwright,
    ):
        config = Path(folder) / "ui.toml"
        config.write_text(
            f'''[[providers]]
name="local-mock"
retries=0
concurrency=1
max_requests=100
[[providers.endpoints]]
id="openai"
protocol="openai_chat"
base_url="{base}/openai/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
[[providers.endpoints]]
id="anthropic"
protocol="anthropic_messages"
base_url="{base}/anthropic/v1"
api_key_env="UI_TEST_PROVIDER_KEY"
''',
            encoding="utf-8",
        )
        browser = playwright.chromium.launch(headless=True)
        try:
            with demo.running_agent(
                config,
                {**os.environ, "UI_TEST_PROVIDER_KEY": "synthetic-key"},
                extra_args=["--data-dir", str(Path(folder) / "data")],
            ) as client:
                token = client.headers["Authorization"].removeprefix("Bearer ")
                page = browser.new_page(accept_downloads=True)
                try:
                    exercise(page, str(client.base_url), token, args.screenshots)
                    exercise_progress(page, str(client.base_url), token)
                finally:
                    release.set()
                    page.close()
            exercise_history(browser.new_page(accept_downloads=True), demo, base, Path(folder))
            exercise_comparison(browser.new_page(), demo, base, Path(folder), catalog_changed)
            exercise_diagnostics(browser.new_page(), demo, base, Path(folder), diagnostic_calls)
            exercise_streaming(browser.new_page(), demo, base, Path(folder))
            exercise_structured(
                browser.new_page(accept_downloads=True), demo, base, Path(folder), structured_calls
            )
            exercise_tool_probe(
                browser.new_page(accept_downloads=True), demo, base, Path(folder), tool_calls
            )
        finally:
            history_release.set()
            browser.close()
    print("Native browser regression passed against synthetic localhost endpoints.")


if __name__ == "__main__":
    main()
