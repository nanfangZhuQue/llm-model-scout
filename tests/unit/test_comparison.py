from copy import deepcopy

import pytest

from llm_model_scout.agent.comparison import ComparisonError, compare_scans

OPENAI = "openai_chat"
ANTHROPIC = "anthropic_messages"


def endpoint(id="openai", protocol=OPENAI):
    return {
        "id": id,
        "protocol": protocol,
        "base_url": "https://provider.example/v1",
        "auth": "default",
        "api_version": "2023-06-01",
        "uses_proxy": False,
        "probe": {
            "profile_id": "chat-basic",
            "max_tokens": 256,
            "temperature": None,
            "prompt": "ping",
        },
    }


def discovery(*models, id="openai", protocol=OPENAI, source="api", status="completed"):
    return {
        "endpoint_id": id,
        "protocol": protocol,
        "source": source,
        "status": status,
        "models": list(models),
    }


def observed(model, status="available", *, id="openai", protocol=OPENAI, error=None):
    return {
        "endpoint_id": id,
        "protocol": protocol,
        "model": model,
        "status": status,
        "probe_id": "chat-basic",
        "error_category": error,
    }


def snapshot(id, *, results=(), discoveries=(), endpoints=None, revision=None, state="completed"):
    return {
        "view": {
            "scan_id": id,
            "provider_id": "p_saved",
            "provider_name": "Saved provider",
            "created_at": "2026-11-26T12:00:00+00:00",
            "state": state,
        },
        "conditions": {
            "provider_revision": revision,
            "endpoints": [endpoint()] if endpoints is None else endpoints,
            "policy": {"timeout": 30, "retries": 2, "max_response_bytes": 1024, "scan_timeout": 60},
        },
        "discovery": list(discoveries),
        "results": list(results),
        "report": None,
    }


def by_model(comparison):
    return {row["model"]: row for row in comparison["rows"]}


def test_state_transitions_and_catalog_membership_are_independent():
    baseline = snapshot(
        "before",
        discoveries=[discovery("regression", "recovery", "same", "absent", "skipped")],
        results=[
            observed("regression"),
            observed("recovery", "unavailable", error="FORBIDDEN"),
            observed("same"),
            observed("absent"),
            observed("skipped"),
        ],
    )
    current = snapshot(
        "after",
        discoveries=[discovery("new", "regression", "recovery", "same", "skipped", "catalog-only")],
        results=[
            observed("new"),
            observed("regression", "unavailable", error="FORBIDDEN"),
            observed("recovery"),
            observed("same"),
            observed("skipped", "skipped"),
        ],
    )
    current["view"]["provider_name"] = "Renamed provider"

    comparison = compare_scans(baseline, current)
    assert comparison["endpoint_id"] == "openai"
    assert comparison["baseline"]["provider_name"] == "Saved provider"
    assert comparison["current"]["provider_name"] == "Renamed provider"
    assert comparison["conditions"] == {
        "status": "unknown",
        "credentials": "unknown",
        "changes": [],
    }
    assert comparison["catalog"] == {
        "baseline": "complete",
        "current": "complete",
        "comparable": True,
    }
    rows = by_model(comparison)
    assert rows["regression"] == {
        "model": "regression",
        "baseline_status": "available",
        "current_status": "unavailable",
        "baseline_error": None,
        "current_error": "FORBIDDEN",
        "verification": "regressed",
        "catalog": None,
    }
    assert rows["recovery"]["verification"] == "recovered"
    assert rows["same"]["verification"] == "unchanged"
    assert rows["new"]["verification"] == "not_tested" and rows["new"]["catalog"] == "added"
    assert rows["absent"]["verification"] == "not_tested" and rows["absent"]["catalog"] == "removed"
    assert rows["absent"]["current_status"] is None
    assert rows["skipped"]["verification"] == "not_tested" and rows["skipped"]["catalog"] is None
    assert rows["skipped"]["current_status"] == "skipped"
    assert rows["catalog-only"]["current_status"] is None


@pytest.mark.parametrize(
    "record,state,expected",
    [
        (discovery("known", source="manual"), "completed", "manual"),
        (discovery("known", status="partial"), "completed", "partial"),
        (discovery(status="failed"), "failed", "partial"),
        (None, "interrupted", "missing"),
    ],
)
def test_incomplete_coverage_does_not_imply_catalog_changes(record, state, expected):
    baseline = snapshot(
        "before",
        discoveries=[discovery("known", "other")],
        results=[observed("other"), observed("known")],
    )
    current = snapshot(
        "after",
        state=state,
        discoveries=[] if record is None else [record],
        results=[observed("known", "unavailable")],
    )
    comparison = compare_scans(baseline, current)
    assert comparison["catalog"] == {
        "baseline": "complete",
        "current": expected,
        "comparable": False,
    }
    rows = by_model(comparison)
    assert rows["other"]["verification"] == "not_tested" and rows["other"]["catalog"] is None
    assert rows["known"]["verification"] == "regressed"


def test_changed_connection_probe_and_policy_disclose_observations_without_verdicts():
    baseline = snapshot(
        "before",
        revision=3,
        discoveries=[discovery("model", "removed")],
        results=[observed("model")],
    )
    current = snapshot(
        "after",
        revision=4,
        discoveries=[discovery("model", "added")],
        results=[observed("model", "unavailable")],
    )
    current["conditions"]["endpoints"][0]["base_url"] = "https://other.example/v1"
    current["conditions"]["endpoints"][0]["probe"]["max_tokens"] = 128
    current["conditions"]["policy"]["timeout"] = 5
    current["conditions"]["policy"]["max_requests"] = 1
    comparison = compare_scans(baseline, current)
    assert comparison["conditions"] == {
        "status": "changed",
        "credentials": "unknown",
        "changes": [
            {"field": "provider_revision", "baseline": 3, "current": 4},
            {
                "field": "endpoint.base_url",
                "baseline": "https://provider.example/v1",
                "current": "https://other.example/v1",
            },
            {"field": "probe.max_tokens", "baseline": 256, "current": 128},
            {"field": "policy.timeout", "baseline": 30, "current": 5},
        ],
    }
    rows = by_model(comparison)
    assert rows["model"]["verification"] == "conditions_changed"
    assert rows["model"]["baseline_status"] == "available"
    assert rows["model"]["current_status"] == "unavailable"
    assert not comparison["catalog"]["comparable"]
    assert all(row["catalog"] is None for row in rows.values())


def test_only_explicitly_selected_protocol_results_are_compared():
    endpoints = [endpoint(), endpoint("anthropic", ANTHROPIC)]
    baseline = snapshot(
        "before",
        revision=1,
        endpoints=endpoints,
        results=[observed("model"), observed("model", id="anthropic", protocol=ANTHROPIC)],
    )
    current = snapshot(
        "after",
        revision=1,
        endpoints=endpoints,
        results=[
            observed("model", "unavailable"),
            observed("model", id="anthropic", protocol=ANTHROPIC),
        ],
    )
    choices = compare_scans(baseline, current)
    assert choices["endpoints"] == [
        {"id": "anthropic", "protocol": ANTHROPIC},
        {"id": "openai", "protocol": OPENAI},
    ]
    assert choices["endpoint_id"] is None and choices["conditions"] is None
    assert choices["rows"] == [] and choices["catalog"] is None
    assert (
        by_model(compare_scans(baseline, current, "openai"))["model"]["verification"] == "regressed"
    )
    comparison = compare_scans(baseline, current, "anthropic")
    assert by_model(comparison)["model"]["verification"] == "unchanged"
    assert comparison["conditions"]["status"] == "matching"
    with pytest.raises(ComparisonError, match="selected endpoint"):
        compare_scans(baseline, current, "missing")


@pytest.mark.parametrize("capability", ["stream", "structured", "tool"])
def test_capability_does_not_pair_with_legacy_text_but_same_capability_does(capability):
    baseline = snapshot("before", revision=1, results=[observed("model")])
    current = snapshot(
        "after",
        revision=1,
        results=[{**observed("model", "unavailable"), "capability": capability}],
    )
    current["conditions"]["endpoints"][0]["probe"]["capability"] = capability

    comparison = compare_scans(baseline, current)
    assert comparison["capabilities"] == {"baseline": "text", "current": capability}
    assert comparison["conditions"]["changes"] == [
        {"field": "probe.capability", "baseline": "text", "current": capability}
    ]
    assert by_model(comparison)["model"]["verification"] == "not_tested"

    baseline["conditions"]["endpoints"][0]["probe"]["capability"] = capability
    baseline["results"][0]["capability"] = capability
    comparison = compare_scans(baseline, current)
    assert comparison["conditions"]["status"] == "matching"
    assert by_model(comparison)["model"]["verification"] == "regressed"


def test_only_effective_authentication_and_version_are_compared():
    openai = endpoint()
    anthropic = endpoint("anthropic", ANTHROPIC)
    baseline = snapshot("before", revision=1, endpoints=[openai, anthropic])
    current = snapshot(
        "after",
        revision=1,
        endpoints=[
            {**openai, "auth": "bearer", "api_version": "2024-01-01"},
            {**anthropic, "auth": "x-api-key", "api_version": "2024-01-01"},
        ],
    )
    assert compare_scans(baseline, current, "openai")["conditions"]["status"] == "matching"
    assert compare_scans(baseline, current, "anthropic")["conditions"]["changes"] == [
        {"field": "endpoint.api_version", "baseline": "2023-06-01", "current": "2024-01-01"}
    ]


@pytest.mark.parametrize("unknown", ["endpoint", "missing_field", "probe_id"])
def test_unknown_effective_conditions_prevent_status_transition_verdicts(unknown):
    baseline = snapshot(
        "before",
        revision=1,
        discoveries=[discovery("model", "other")],
        results=[observed("model"), observed("other")],
    )
    current = snapshot(
        "after",
        revision=1,
        discoveries=[discovery("model")],
        results=[observed("model", "unavailable")],
    )
    if unknown == "endpoint":
        current["conditions"]["endpoints"][0]["base_url"] = "https://***REDACTED***/v1"
    elif unknown == "missing_field":
        del current["conditions"]["endpoints"][0]["probe"]["max_tokens"]
    else:
        del current["results"][0]["probe_id"]

    comparison = compare_scans(baseline, current)
    rows = by_model(comparison)
    assert rows["model"]["verification"] == "conditions_unknown"
    assert rows["model"]["baseline_status"] == "available"
    assert rows["model"]["current_status"] == "unavailable"
    assert rows["other"]["verification"] == "not_tested"
    if unknown != "probe_id":
        assert comparison["conditions"]["status"] == "unknown"
        assert not comparison["catalog"]["comparable"]


def test_redacted_or_ambiguous_models_and_endpoints_are_not_paired():
    baseline = snapshot(
        "before",
        endpoints=[endpoint(), endpoint("***REDACTED***")],
        discoveries=[discovery("valid", "ambiguous", "***REDACTED***")],
        results=[
            observed("valid"),
            observed("ambiguous"),
            observed("ambiguous", "unavailable"),
            observed("***REDACTED***"),
        ],
    )
    current = deepcopy(baseline)
    current["view"]["scan_id"] = "after"
    comparison = compare_scans(baseline, current)
    assert comparison["endpoints"] == [{"id": "openai", "protocol": OPENAI}]
    assert comparison["catalog"] == {
        "baseline": "partial",
        "current": "partial",
        "comparable": False,
    }
    assert set(by_model(comparison)) == {"valid"}


@pytest.mark.parametrize(
    "field,value,error",
    [
        ("scan_id", "before", "different scans"),
        ("state", "running", "must have ended"),
        ("provider_id", "another-provider", "same identifiable provider"),
    ],
)
def test_invalid_scan_pairs_fail_clearly(field, value, error):
    before, after = snapshot("before"), snapshot("after")
    after["view"][field] = value
    with pytest.raises(ComparisonError, match=error):
        compare_scans(before, after)


def test_endpoint_id_with_changed_protocol_is_not_comparable():
    before = snapshot("before")
    after = snapshot("after", endpoints=[endpoint("openai", ANTHROPIC)])
    with pytest.raises(ComparisonError, match="no identifiable endpoint"):
        compare_scans(before, after)
