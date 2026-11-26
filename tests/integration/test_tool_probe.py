import json

import httpx
import pytest

from llm_model_scout.core.scanner import scan
from llm_model_scout.domain import EndpointConfig, ProbeConfig, ProviderConfig, ScanPolicy
from llm_model_scout.report import report_json


def _provider():
    endpoint = EndpointConfig(
        "messages",
        "anthropic_messages",
        "https://provider.example/v1",
        "synthetic-test-key",
        models=("example-model",),
        probe=ProbeConfig(capability="tool"),
    )
    return ProviderConfig("Example", (endpoint,), ScanPolicy(retries=0, max_requests=1))


@pytest.mark.parametrize(
    ("raw_count", "diagnostic"),
    [
        (b"1.0", None),
        (b"1.00000000000000000001", "invalid_tool_arguments"),
        (b'1,"count":1', "invalid_structure"),
    ],
)
async def test_native_tool_input_decoding_preserves_json_number_and_member_semantics(
    raw_count, diagnostic
):
    calls = []
    response = {
        "type": "message",
        "role": "assistant",
        "stop_reason": "tool_use",
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_test",
                "name": "scout_tool_probe",
                "input": {"value": "ping", "count": "RAW_NUMBER"},
            }
        ],
    }
    body = json.dumps(response).encode().replace(b'"RAW_NUMBER"', raw_count)

    def upstream(request):
        calls.append(request)
        return httpx.Response(200, content=body)

    report = await scan(_provider(), http_transport=httpx.MockTransport(upstream))
    (row,) = report.results
    assert row.diagnostic_code == diagnostic
    assert row.status == ("available" if diagnostic is None else "unavailable")
    assert len(calls) == row.attempts == report.requests_sent == 1
    assert calls[0].url.path == "/v1/messages"
    assert "toolu_test" not in report_json(report)


async def test_tool_request_error_reports_status_without_upstream_body():
    def upstream(_request):
        return httpx.Response(
            400, json={"error": {"message": "echoed private-argument-sample", "type": "bad_input"}}
        )

    report = await scan(_provider(), http_transport=httpx.MockTransport(upstream))
    (row,) = report.results
    assert row.http_status == 400 and row.status == "unavailable"
    assert row.error_message == "Provider returned HTTP 400 for the tool probe"
    assert "private-argument-sample" not in report_json(report)
