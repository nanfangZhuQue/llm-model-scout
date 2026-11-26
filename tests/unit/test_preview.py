import pytest

from llm_model_scout.agent.preview import scan_preview
from llm_model_scout.domain import EndpointConfig, ProbeConfig, ProviderConfig, ScanPolicy


def endpoint(id, models, *, protocol="openai_chat", max_tokens=16):
    return EndpointConfig(
        id,
        protocol,
        "https://provider.example/v1",
        "synthetic-secret",
        proxy="http://user:password@proxy.example",
        models=models,
        probe=ProbeConfig("chat-basic", max_tokens, None, "internal probe"),
    )


def test_single_explicit_model_has_one_request_and_no_connection_data():
    provider = ProviderConfig(
        "Example",
        (endpoint("chat", ("model-a",), max_tokens=48),),
        ScanPolicy(max_requests=1, retries=0, timeout=2, scan_timeout=10),
    )

    assert scan_preview(provider, mode="quick", operation="scan", requests_remaining=99) == {
        "mode": "quick",
        "operation": "scan",
        "capability": "text",
        "endpoints": [
            {
                "id": "chat",
                "protocol": "openai_chat",
                "max_tokens": 48,
                "target_count": 1,
                "source": "manual",
            }
        ],
        "target_count": 1,
        "max_models": 1000,
        "max_requests": 1,
        "requests_remaining": 99,
        "effective_max_requests": 1,
        "retries": 0,
        "scan_timeout": 10,
        "timeout": 2,
        "request_upper_bound": 1,
    }


@pytest.mark.parametrize(("max_models", "expected"), [(9, 4), (3, 3)])
def test_manual_batch_deduplicates_per_endpoint_and_applies_model_limit(max_models, expected):
    provider = ProviderConfig(
        "Example",
        (
            endpoint("chat", ("shared", "chat-only", "chat-only")),
            endpoint("messages", ("shared", "messages-only"), protocol="anthropic_messages"),
        ),
        ScanPolicy(max_models=max_models, max_requests=20, retries=2),
    )

    preview = scan_preview(provider, mode="batch", operation="scan", requests_remaining=20)

    assert preview["target_count"] == expected
    assert [item["target_count"] for item in preview["endpoints"]] == [2, 2]
    assert preview["request_upper_bound"] == expected * 3


@pytest.mark.parametrize(
    ("operation", "configured", "targets", "upper_bound"),
    [("scan", ("configured",), None, 14), ("models", None, 0, 16)],
)
def test_api_discovery_includes_page_attempts_in_request_bound(
    operation, configured, targets, upper_bound
):
    provider = ProviderConfig(
        "Example",
        (endpoint("chat", configured), endpoint("discovered", None)),
        ScanPolicy(max_models=3, max_pages=4, max_requests=20, retries=1),
    )

    preview = scan_preview(provider, mode="batch", operation=operation, requests_remaining=50)

    assert preview["target_count"] == targets
    assert [(item["source"], item["target_count"]) for item in preview["endpoints"]] == [
        ("api", None) if configured is None else ("manual", 1),
        ("api", None),
    ]
    assert preview["request_upper_bound"] == upper_bound


@pytest.mark.parametrize(("remaining", "cap"), [(10, 5), (3, 3), (0, 0), (-1, 0)])
def test_request_bound_respects_task_and_shared_quota(remaining, cap):
    provider = ProviderConfig(
        "Example",
        (endpoint("chat", None),),
        ScanPolicy(max_requests=5, max_models=3, max_pages=4, retries=1),
    )

    preview = scan_preview(provider, mode="batch", operation="scan", requests_remaining=remaining)

    assert preview["requests_remaining"] == max(0, remaining)
    assert preview["effective_max_requests"] == cap
    assert preview["request_upper_bound"] == cap
