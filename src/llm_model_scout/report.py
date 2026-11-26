import csv
import io
import json
import os
import tempfile
import unicodedata
from collections import Counter
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from . import __version__
from .domain import ScanReport


def report_dict(report: ScanReport) -> dict:
    counts = Counter(r.status for r in report.results)
    return {
        "schema_version": 1,
        "tool": {"name": "model-scout", "version": __version__},
        "provider": {"name": report.provider, "endpoints": report.endpoints},
        "scan": {
            "status": report.status,
            "reason": report.reason,
            "started_at": report.started_at,
            "finished_at": report.finished_at,
            "requests_sent": report.requests_sent,
            "policy": report.policy,
        },
        "discovery": [asdict(d) for d in report.discovery],
        "results": [r.record() for r in report.results],
        "summary": {
            "total": len(report.results),
            **{
                status: counts[status]
                for status in ("available", "unavailable", "skipped", "cancelled")
            },
        },
    }


def report_json(report: ScanReport) -> str:
    return json.dumps(report_dict(report), ensure_ascii=False, indent=2, allow_nan=False)


CSV_FIELDS = (
    "csv_schema_version",
    "tool_version",
    "provider",
    "endpoint_id",
    "protocol",
    "base_url",
    "scan_status",
    "scan_reason",
    "model",
    "probe_id",
    "max_tokens",
    "status",
    "http_status",
    "error_category",
    "error_message",
    "reason",
    "diagnostic_code",
    "suggestion",
    "stop_reason",
    "attempts",
    "latency_ms",
    "elapsed_ms",
    "timestamp",
)

CSV_FIELD_LABELS = {
    "csv_schema_version": "CSV架构版本",
    "tool_version": "工具版本",
    "provider": "服务商",
    "endpoint_id": "入口ID",
    "protocol": "协议",
    "base_url": "基础URL",
    "scan_status": "扫描状态",
    "scan_reason": "扫描原因",
    "model": "模型",
    "probe_id": "探测配置",
    "max_tokens": "最大Token数",
    "status": "模型状态",
    "http_status": "HTTP状态码",
    "error_category": "错误分类",
    "error_message": "错误信息",
    "reason": "失败原因",
    "diagnostic_code": "诊断代码",
    "suggestion": "建议",
    "stop_reason": "停止原因",
    "attempts": "尝试次数",
    "latency_ms": "延迟(毫秒)",
    "elapsed_ms": "耗时(毫秒)",
    "timestamp": "时间戳",
}


def _csv_cell(value):
    if not isinstance(value, str) or not value:
        return value
    # Quoting alone does not prevent spreadsheet formula execution. Keep JSON unchanged.
    first = next(
        (
            c
            for c in unicodedata.normalize("NFKC", value)
            if not c.isspace() and ord(c) >= 32 and unicodedata.category(c) != "Cf"
        ),
        "",
    )
    if value[0] in "\t\r\n" or (first and first in "=+-@"):
        return "'" + value
    return value


def _probe_label(result):
    if result.capability == "text":
        return result.probe_id
    return f"{result.probe_id} ({result.capability})"


def report_csv(report: ScanReport, *, tool_version=__version__) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=CSV_FIELDS, lineterminator="\r\n")
    writer.writerow({key: CSV_FIELD_LABELS.get(key, key) for key in CSV_FIELDS})
    endpoints = {e["id"]: e for e in report.endpoints}
    for result in sorted(report.results, key=lambda r: (r.endpoint_id, r.model, r.probe_id)):
        endpoint = endpoints.get(result.endpoint_id, {})
        row = {
            "csv_schema_version": 1,
            "tool_version": tool_version,
            "provider": report.provider,
            "base_url": endpoint.get("base_url", ""),
            "scan_status": report.status,
            "scan_reason": report.reason,
            "max_tokens": endpoint.get("probe", {}).get("max_tokens"),
            **asdict(result),
            "probe_id": _probe_label(result),
        }
        writer.writerow({key: _csv_cell(row.get(key)) for key in CSV_FIELDS})
    return output.getvalue()


XLSX_EXCLUDED_FIELDS = ("csv_schema_version", "tool_version", "suggestion")
XLSX_FIELDS = tuple(f for f in CSV_FIELDS if f not in XLSX_EXCLUDED_FIELDS)
XLSX_HEADER_LABELS = dict(CSV_FIELD_LABELS)
XLSX_HEADER_LABELS["latency_ms"] = "延迟(秒)"
XLSX_HEADER_LABELS["elapsed_ms"] = "耗时(秒)"
XLSX_HEADER_LABELS["timestamp"] = "时间"

XLSX_VALUE_LABELS = {
    "scan_status": {
        "completed": "已完成",
        "failed": "失败",
        "cancelled": "已取消",
        "interrupted": "已中断",
    },
    "scan_reason": {
        "agent_interrupted": "Agent 进程中断，仅含已保存结果",
        "checkpoint_only": "仅含已保存结果",
    },
    "status": {
        "available": "通过",
        "unavailable": "未通过",
        "skipped": "未执行",
        "cancelled": "已取消",
    },
    "error_category": {
        "OK": "正常",
        "NETWORK_ERROR": "网络错误",
        "TIMEOUT": "超时",
        "UNAUTHORIZED": "未授权",
        "FORBIDDEN": "禁止访问",
        "PAYMENT_REQUIRED": "需付费",
        "MODEL_NOT_FOUND": "模型不存在",
        "RATE_LIMITED": "限流",
        "SERVER_ERROR": "服务器错误",
        "BAD_RESPONSE": "响应异常",
        "UNKNOWN_ERROR": "未知错误",
    },
    "reason": {
        "probe_passed": "探测通过",
        "probe_failed": "探测失败",
        "not_started": "未开始",
        "not_executed": "未执行",
        "user_cancelled": "用户取消",
        "scan_deadline_exceeded": "扫描超时",
        "request_budget_exhausted": "请求预算耗尽",
        "model_budget_exhausted": "模型预算耗尽",
        "discovery_incomplete": "发现未完成",
        "scan_incomplete": "扫描未完成",
        "no_candidates_after_filtering": "过滤后无候选",
    },
    "stop_reason": {
        "completed": "正常结束",
        "incomplete": "响应未完成",
        "in_progress": "响应处理中",
        "queued": "响应排队中",
        "failed": "响应失败",
        "cancelled": "响应已取消",
        "max_output_tokens": "输出长度限制",
        "end_turn": "正常结束",
        "stop": "正常结束",
        "max_tokens": "长度限制",
        "length": "长度限制",
        "stop_sequence": "停止序列",
        "tool_use": "工具调用",
        "tool_calls": "工具调用",
        "function_call": "函数调用",
        "content_filter": "内容过滤",
        "pause_turn": "暂停",
        "refusal": "拒绝回答",
        "model_context_window_exceeded": "上下文超限",
    },
    "protocol": {
        "openai_chat": "OpenAI Chat",
        "anthropic_messages": "Anthropic Messages",
        "openai_responses": "OpenAI Responses",
    },
}


def _xlsx_timestamp(value) -> str:
    try:
        local = datetime.fromisoformat(str(value)).astimezone()
        return local.strftime("%Y-%m-%d %H:%M:%S")
    except (ValueError, TypeError):
        return str(value or "")


def report_xlsx(report: ScanReport) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "测试结果"
    header_fill = PatternFill("solid", fgColor="1F4E79")
    header_font = Font(bold=True, color="FFFFFF")
    pass_fill = PatternFill("solid", fgColor="C6EFCE")
    bold_green = Font(color="006100", bold=True)
    center = Alignment(horizontal="center", vertical="center")
    sheet.append([XLSX_HEADER_LABELS.get(key, key) for key in XLSX_FIELDS])
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = center
    sheet.freeze_panes = "A2"
    endpoints = {e["id"]: e for e in report.endpoints}
    for result in sorted(report.results, key=lambda r: (r.endpoint_id, r.model, r.probe_id)):
        endpoint = endpoints.get(result.endpoint_id, {})
        row = {
            "provider": report.provider,
            "base_url": endpoint.get("base_url", ""),
            "scan_status": report.status,
            "scan_reason": report.reason,
            "max_tokens": endpoint.get("probe", {}).get("max_tokens"),
            **asdict(result),
            "probe_id": _probe_label(result),
        }
        values = []
        for key in XLSX_FIELDS:
            value = row.get(key)
            if key == "timestamp":
                value = _xlsx_timestamp(value)
            elif key in ("latency_ms", "elapsed_ms"):
                value = round(value / 1000, 3) if isinstance(value, (int, float)) else None
            elif key in XLSX_VALUE_LABELS and value in XLSX_VALUE_LABELS[key]:
                value = XLSX_VALUE_LABELS[key][value]
            values.append(value)
        sheet.append(values)
        for cell in sheet[sheet.max_row]:
            # Preserve upstream strings as data, including values that resemble formulas.
            if isinstance(cell.value, str):
                cell.data_type = "s"
            if result.status == "available":
                cell.fill = pass_fill
        if result.status == "available":
            sheet.cell(row=sheet.max_row, column=XLSX_FIELDS.index("status") + 1).font = bold_green
    sheet.auto_filter.ref = sheet.dimensions
    widths = {
        "provider": 14,
        "endpoint_id": 10,
        "protocol": 18,
        "base_url": 30,
        "scan_status": 10,
        "scan_reason": 16,
        "model": 34,
        "probe_id": 12,
        "max_tokens": 11,
        "status": 9,
        "http_status": 9,
        "error_category": 12,
        "error_message": 40,
        "reason": 14,
        "diagnostic_code": 13,
        "stop_reason": 11,
        "attempts": 8,
        "latency_ms": 10,
        "elapsed_ms": 10,
        "timestamp": 20,
    }
    for index, key in enumerate(XLSX_FIELDS, 1):
        sheet.column_dimensions[sheet.cell(row=1, column=index).column_letter].width = widths.get(
            key, 12
        )
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def write_report(report: ScanReport, path: Path, output_format: str = "json") -> None:
    if output_format not in ("json", "csv"):
        raise ValueError("Output format must be json or csv")
    content = report_csv(report) if output_format == "csv" else report_json(report) + "\n"
    encoding = "utf-8-sig" if output_format == "csv" else "utf-8"
    path = Path(path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding=encoding, newline="", dir=path.parent, prefix=".scout-", delete=False
        ) as handle:
            temp_path = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        if temp_path and os.path.exists(temp_path):
            os.unlink(temp_path)
