import io
from datetime import datetime

from openpyxl import load_workbook

from llm_model_scout.domain import ModelResult, ScanReport
from llm_model_scout.report import report_xlsx


def example_report():
    ok = ModelResult(
        "e",
        "openai_chat",
        "model-ok",
        "chat-basic",
        status="available",
        http_status=200,
        error_category="OK",
        reason="probe_passed",
        attempts=1,
        latency_ms=1234.0,
        elapsed_ms=1300.0,
        stop_reason="end_turn",
    )
    bad = ModelResult(
        "e",
        "openai_chat",
        "model-bad",
        "chat-basic",
        status="unavailable",
        http_status=402,
        error_category="PAYMENT_REQUIRED",
        error_message="Insufficient credits",
        reason="probe_failed",
        attempts=2,
        latency_ms=700.0,
        elapsed_ms=710.5,
    )
    return ScanReport(
        "vendor",
        [
            {
                "id": "e",
                "protocol": "openai_chat",
                "base_url": "https://example.test/v1",
                "probe": {"max_tokens": 128},
            }
        ],
        {},
        status="failed",
        reason="scan_incomplete",
        results=[ok, bad],
    )


def sheet():
    book = load_workbook(io.BytesIO(report_xlsx(example_report())))
    return book.active


def test_headers_exclude_version_and_suggestion_columns():
    header = [cell.value for cell in next(sheet().iter_rows(max_row=1))]
    for banned in ("CSV架构版本", "工具版本", "建议"):
        assert banned not in header
    assert header[0] == "服务商"
    assert "延迟(秒)" in header and "耗时(秒)" in header and "时间" in header


def test_values_translated_and_units_in_seconds():
    rows = list(sheet().iter_rows(min_row=2, values_only=True))
    header = [cell.value for cell in next(sheet().iter_rows(max_row=1))]
    by_model = {row[header.index("模型")]: row for row in rows}
    ok = by_model["model-ok"]
    assert ok[header.index("扫描状态")] == "失败"
    assert ok[header.index("模型状态")] == "通过"
    assert ok[header.index("错误分类")] == "正常"
    assert ok[header.index("失败原因")] == "探测通过"
    assert ok[header.index("停止原因")] == "正常结束"
    assert ok[header.index("延迟(秒)")] == 1.234
    assert ok[header.index("耗时(秒)")] == 1.3
    bad = by_model["model-bad"]
    assert bad[header.index("模型状态")] == "未通过"
    assert bad[header.index("错误分类")] == "需付费"


def test_timestamp_formatted_to_local_seconds():
    rows = list(sheet().iter_rows(min_row=2, values_only=True))
    header = [cell.value for cell in next(sheet().iter_rows(max_row=1))]
    stamp = rows[0][header.index("时间")]
    datetime.strptime(stamp, "%Y-%m-%d %H:%M:%S")


def test_freeze_header_and_highlight_passed_rows():
    page = sheet()
    assert page.freeze_panes == "A2"
    assert page.auto_filter.ref == page.dimensions == "A1:T3"
    header = [cell.value for cell in next(page.iter_rows(max_row=1))]
    model_col = header.index("模型")
    status_col = header.index("模型状态")
    for row in page.iter_rows(min_row=2):
        if row[model_col].value == "model-ok":
            assert row[status_col].value == "通过"
            assert row[model_col].fill.fgColor.rgb.endswith("C6EFCE")
        else:
            assert not row[model_col].fill.fgColor.rgb.endswith("C6EFCE")


def test_formula_like_labels_stay_literal_text_in_xlsx():
    report = example_report()
    report.provider = "=1+1"
    report.results[0].model = "=2+2"
    report.results[0].capability = "structured"
    book = load_workbook(io.BytesIO(report_xlsx(report)))
    try:
        headers = [cell.value for cell in book.active[1]]
        first = book.active[2]
        assert first[headers.index("服务商")].value == "=1+1"
        assert first[headers.index("模型")].value == "=2+2"
        assert first[headers.index("探测配置")].value == "chat-basic (structured)"
        assert first[headers.index("服务商")].data_type == "s"
        assert first[headers.index("模型")].data_type == "s"
    finally:
        book.close()
