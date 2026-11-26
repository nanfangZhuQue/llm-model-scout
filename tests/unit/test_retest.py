from dataclasses import replace

import pytest

from llm_model_scout.agent.comparison import ComparisonError
from llm_model_scout.agent.retest import RetestConnectionError, RetestError, prepare_retest
from llm_model_scout.domain import EndpointConfig, ProbeConfig, ProviderConfig

OPENAI = "openai_chat"
ANTHROPIC = "anthropic_messages"
BASE = "https://provider.example/v1"
KEY = "synthetic-live-api-key"
OLD_PROBE = ProbeConfig("chat-basic", 131072, 0.25, "saved probe text")


def historical_endpoint(endpoint_id, protocol):
    return {
        "id": endpoint_id,
        "protocol": protocol,
        "base_url": BASE,
        "auth": "default",
        "api_version": "2023-06-01",
        "uses_proxy": False,
        "probe": {
            "profile_id": OLD_PROBE.profile_id,
            "max_tokens": OLD_PROBE.max_tokens,
            "temperature": OLD_PROBE.temperature,
            "prompt": OLD_PROBE.prompt,
        },
    }


def observation(model, status, *, endpoint_id="openai", protocol=OPENAI):
    return {
        "endpoint_id": endpoint_id,
        "protocol": protocol,
        "model": model,
        "status": status,
        "probe_id": "chat-basic",
    }


@pytest.fixture
def snapshots():
    def snapshot(scan_id, regression, recovery):
        return {
            "view": {
                "scan_id": scan_id,
                "provider_id": "p_saved",
                "provider_name": "Saved provider",
                "created_at": "2026-11-26T12:00:00+00:00",
                "state": "completed",
            },
            "conditions": {
                "provider_revision": None,
                "endpoints": [
                    historical_endpoint("openai", OPENAI),
                    historical_endpoint("anthropic", ANTHROPIC),
                ],
            },
            "discovery": [
                {
                    "endpoint_id": "openai",
                    "protocol": OPENAI,
                    "source": "api",
                    "status": "completed",
                    "models": ["regression", "recovery", "stable", " untested "],
                }
            ],
            "results": [
                observation("regression", regression),
                observation("recovery", recovery),
                observation("stable", "available"),
                observation(
                    "other-endpoint", regression, endpoint_id="anthropic", protocol=ANTHROPIC
                ),
            ],
        }

    return snapshot("base", "available", "unavailable"), snapshot(
        "current", "unavailable", "available"
    )


@pytest.fixture
def provider():
    return ProviderConfig(
        "Current provider",
        (
            EndpointConfig("openai", OPENAI, BASE, KEY),
            EndpointConfig("anthropic", ANTHROPIC, BASE, KEY),
        ),
    )


def test_reuses_exact_saved_probe_and_deduplicates_selected_models(snapshots, provider):
    plan = prepare_retest(
        *snapshots, "openai", ["regression", " untested ", "regression"], provider, None
    )

    assert plan.endpoint_id == "openai"
    assert plan.models == ("regression", " untested ")
    assert plan.probe == OLD_PROBE
    assert plan.probe != provider.endpoints[0].probe
    assert plan.changes == []


@pytest.mark.parametrize("capability", ["stream", "structured", "tool"])
def test_retest_reuses_current_capability(snapshots, provider, capability):
    current = snapshots[1]
    current["conditions"]["endpoints"][0]["probe"]["capability"] = capability
    current["results"][0]["capability"] = capability
    plan = prepare_retest(*snapshots, "openai", ["regression"], provider, None)
    assert plan.probe == replace(OLD_PROBE, capability=capability)
    assert plan.changes == []


@pytest.mark.parametrize("selection", [[], ["unknown"], ["stable"], ["other-endpoint"]])
def test_rejects_targets_outside_selected_endpoint_differences(snapshots, provider, selection):
    with pytest.raises(RetestError, match="changed or untested"):
        prepare_retest(*snapshots, "openai", selection, provider, None)


@pytest.mark.parametrize("protocol", [None, ANTHROPIC])
def test_missing_or_replaced_live_endpoint_is_not_retestable(snapshots, provider, protocol):
    endpoints = (
        provider.endpoints[1:]
        if protocol is None
        else (replace(provider.endpoints[0], protocol=protocol), *provider.endpoints[1:])
    )
    with pytest.raises(RetestConnectionError, match="no longer available"):
        prepare_retest(
            *snapshots, "openai", ["regression"], replace(provider, endpoints=endpoints), None
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("profile_id", "custom"),
        ("max_tokens", True),
        ("max_tokens", 0),
        ("temperature", float("nan")),
        ("prompt", "hello ***REDACTED***"),
    ],
)
def test_invalid_or_redacted_saved_probe_is_rejected(snapshots, provider, field, value):
    snapshots[1]["conditions"]["endpoints"][0]["probe"][field] = value
    with pytest.raises(RetestError, match="cannot be reused"):
        prepare_retest(*snapshots, "openai", ["regression"], provider, None)


def test_uses_current_snapshot_and_shows_connection_revision_changes(snapshots, provider):
    baseline, current = snapshots
    baseline["conditions"]["endpoints"][0]["base_url"] = "https://older.example/v1"
    current["conditions"]["provider_revision"] = 5
    changed = replace(
        provider.endpoints[0],
        base_url="https://new.example/v1",
        auth="x-api-key",
        proxy="http://user:synthetic-proxy-password@127.0.0.1:8080",
    )
    provider = replace(provider, endpoints=(changed, *provider.endpoints[1:]))

    plan = prepare_retest(baseline, current, "openai", ["recovery"], provider, 6)
    assert {
        change["field"]: (change["baseline"], change["current"]) for change in plan.changes
    } == {
        "endpoint.base_url": (BASE, "https://new.example/v1"),
        "endpoint.auth": ("bearer", "x-api-key"),
        "endpoint.uses_proxy": (False, True),
        "provider_revision": (5, 6),
    }
    assert KEY not in str(plan.changes)
    assert "synthetic-proxy-password" not in str(plan.changes)


def test_changed_version_for_anthropic_and_normalized_auth_for_openai(snapshots, provider):
    openai, anthropic = provider.endpoints
    provider = replace(
        provider,
        endpoints=(replace(openai, auth="bearer"), replace(anthropic, api_version="2024-01-01")),
    )
    assert prepare_retest(*snapshots, "openai", ["regression"], provider, None).changes == []
    assert prepare_retest(*snapshots, "anthropic", ["other-endpoint"], provider, None).changes == [
        {"field": "endpoint.api_version", "baseline": "2023-06-01", "current": "2024-01-01"}
    ]


def test_unavailable_connection_fields_require_confirmation_without_leaking_secrets(
    snapshots, provider
):
    previous = snapshots[1]["conditions"]["endpoints"][0]
    previous["base_url"] = "https://***REDACTED***.example/v1"
    previous.pop("uses_proxy")
    provider = replace(
        provider,
        endpoints=(
            replace(provider.endpoints[0], base_url=f"https://provider.example/{KEY}"),
            provider.endpoints[1],
        ),
    )
    plan = prepare_retest(*snapshots, "openai", ["regression"], provider, None)
    assert plan.changes == [
        {"field": "endpoint.base_url", "baseline": None, "current": None},
        {"field": "endpoint.uses_proxy", "baseline": None, "current": False},
    ]
    assert KEY not in str(plan.changes)


def test_rejects_incompatible_comparison(snapshots, provider):
    snapshots[0]["view"]["provider_id"] = "different"
    with pytest.raises(ComparisonError, match="same identifiable provider"):
        prepare_retest(*snapshots, "openai", ["regression"], provider, None)
