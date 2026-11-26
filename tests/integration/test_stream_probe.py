import csv
import json
from io import BytesIO, StringIO

import httpx
import pytest
from openpyxl import load_workbook

from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProbeConfig, ProviderConfig, ScanPolicy
from llm_model_scout.report import report_csv, report_dict, report_xlsx

HEADERS = {"Content-Type": "text/event-stream; charset=utf-8"}
TEXT = b'data: {"choices":[{"index":0,"delta":{"content":"pong"}}]}\n\n'
FINISH = b'data: {"choices":[{"index":0,"delta":{},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'


def provider(*, retries=0):
    endpoint = EndpointConfig(
        "chat",
        "openai_chat",
        "https://provider.example/v1",
        "synthetic-test-key-value",
        models=("model",),
        probe=ProbeConfig(capability="stream"),
    )
    return ProviderConfig("Demo", (endpoint,), ScanPolicy(retries=retries, backoff_base=0))


async def test_stream_retry_starts_over_without_text_from_timed_out_attempt():
    requests = []
    closed = []

    class TimedOutStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield TEXT
            raise httpx.ReadTimeout("Test timeout after text")

        async def aclose(self):
            closed.append(True)

    def upstream(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(200, headers=HEADERS, stream=TimedOutStream())
        return httpx.Response(200, headers=HEADERS, content=FINISH)

    report = await scan(provider(retries=1), http_transport=httpx.MockTransport(upstream))
    (row,) = report.results
    assert row.status == "unavailable" and row.diagnostic_code == "empty_stream"
    assert row.attempts == report.requests_sent == 2 and len(requests) == 2
    assert closed == [True]
    assert all(request.headers["accept-encoding"] == "identity" for request in requests)
    assert all(json.loads(request.content)["stream"] is True for request in requests)


@pytest.mark.parametrize(
    "headers", [{"content-type": "application/json"}, {**HEADERS, "content-encoding": "gzip"}]
)
async def test_stream_transport_requires_uncompressed_event_stream(headers):
    def upstream(_):
        return httpx.Response(200, headers=headers, stream=httpx.ByteStream(TEXT + FINISH))

    report = await scan(provider(), http_transport=httpx.MockTransport(upstream))
    (row,) = report.results
    assert row.status == "unavailable" and row.diagnostic_code == "invalid_stream"
    assert row.http_status == 200 and report.requests_sent == 1


async def test_stream_capability_is_preserved_in_reports():
    def upstream(_):
        return httpx.Response(200, headers=HEADERS, content=TEXT + FINISH)

    report = await scan(provider(), http_transport=httpx.MockTransport(upstream))
    result = report_dict(report)["results"][0]
    assert result["capability"] == "stream" and result["probe_id"] == "baseline"
    (row,) = csv.DictReader(StringIO(report_csv(report)))
    assert row["探测配置"] == "baseline (stream)"
    workbook = load_workbook(BytesIO(report_xlsx(report)), read_only=True)
    try:
        columns, values = workbook.active.values
        assert values[columns.index("探测配置")] == "baseline (stream)"
    finally:
        workbook.close()
