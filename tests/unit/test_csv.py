import csv
import io
import json
from dataclasses import asdict

import pytest

from llm_model_scout.domain import ModelResult, ScanReport
from llm_model_scout.report import (
    CSV_FIELD_LABELS,
    CSV_FIELDS,
    report_csv,
    report_json,
    write_report,
)


def example_report():
    result = ModelResult(
        "e",
        "openai_chat",
        "模型,a",
        "chat-basic",
        status="unavailable",
        error_message='provider said "no"\ntry later',
        reason="probe_failed",
        diagnostic_code="empty_text",
        attempts=1,
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
        results=[result],
    )


def test_csv_roundtrip_and_bom(tmp_path):
    report = example_report()
    path = tmp_path / "result.csv"
    write_report(report, path, output_format="csv")
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    rows = list(csv.DictReader(io.StringIO(path.read_text(encoding="utf-8-sig"))))
    assert list(rows[0]) == [CSV_FIELD_LABELS[f] for f in CSV_FIELDS]
    assert rows[0][CSV_FIELD_LABELS["model"]] == "模型,a"
    assert rows[0][CSV_FIELD_LABELS["error_message"]] == 'provider said "no"\ntry later'
    assert rows[0][CSV_FIELD_LABELS["max_tokens"]] == "128"
    assert rows[0][CSV_FIELD_LABELS["http_status"]] == ""
    assert rows[0][CSV_FIELD_LABELS["attempts"]] == "1"


@pytest.mark.parametrize(
    "value", ["=1+1", "+cmd", "-1+2", "@SUM(A1)", "  =X", "\tfoo", "\rfoo", "\nfoo", "﻿=1+1", "​=1+1"]
)
def test_formula_neutralization_without_mutation(value):
    report = example_report()
    report.provider = value
    report.results[0].model = value
    before = asdict(report)
    text = report_csv(report)
    row = next(csv.DictReader(io.StringIO(text)))
    assert row[CSV_FIELD_LABELS["provider"]] == "'" + value
    assert row[CSV_FIELD_LABELS["model"]] == "'" + value
    assert asdict(report) == before
    assert json.loads(report_json(report))["results"][0]["model"] == value


def test_empty_csv_has_header_only():
    report = ScanReport("p", [], {}, status="failed", reason="discovery_incomplete")
    rows = list(csv.reader(io.StringIO(report_csv(report))))
    assert rows == [[CSV_FIELD_LABELS[f] for f in CSV_FIELDS]]


def test_partial_report_preserves_scan_state_and_numeric_cells():
    report = example_report()
    report.status = "cancelled"
    report.reason = "user_cancelled"
    report.results[0].status = "cancelled"
    report.results[0].latency_ms = 12.5
    row = next(csv.DictReader(io.StringIO(report_csv(report))))
    assert row[CSV_FIELD_LABELS["scan_status"]] == row[CSV_FIELD_LABELS["status"]] == "cancelled"
    assert row[CSV_FIELD_LABELS["scan_reason"]] == "user_cancelled"
    assert row[CSV_FIELD_LABELS["latency_ms"]] == "12.5"


def test_invalid_format_does_not_replace_existing_file(tmp_path):
    path = tmp_path / "existing.txt"
    path.write_text("keep", encoding="utf-8")
    with pytest.raises(ValueError):
        write_report(example_report(), path, output_format="xlsx")
    assert path.read_text() == "keep"


def test_default_writer_stays_json(tmp_path):
    path = tmp_path / "result.json"
    report = example_report()
    write_report(report, path)
    assert path.read_text(encoding="utf-8") == report_json(report) + "\n"
