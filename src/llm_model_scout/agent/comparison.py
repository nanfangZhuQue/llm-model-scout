"""Compare endpoint observations from two retained scan snapshots."""

from collections import Counter

from ..domain import DEFAULT_AUTH

_TERMINAL = {"completed", "failed", "cancelled", "interrupted"}
_TESTED = {"available", "unavailable"}
_RUN_FIELDS = ("scan_id", "provider_id", "provider_name", "created_at", "state")
_ENDPOINT_FIELDS = ("base_url", "auth", "uses_proxy")
_PROBE_FIELDS = ("profile_id", "max_tokens", "temperature", "prompt")
_POLICY_FIELDS = ("timeout", "retries", "max_response_bytes", "scan_timeout")
_MISSING = object()
_REDACTION = "***REDACTED***"


class ComparisonError(ValueError):
    """The selected snapshots cannot be paired safely."""


def _redacted(value):
    return isinstance(value, str) and _REDACTION in value


def _identifiable(value):
    return isinstance(value, str) and bool(value) and not _redacted(value)


def _endpoints(snapshot):
    endpoints = snapshot["conditions"]["endpoints"]
    counts = Counter(endpoint["id"] for endpoint in endpoints)
    return {
        endpoint["id"]: endpoint
        for endpoint in endpoints
        if _identifiable(endpoint["id"]) and counts[endpoint["id"]] == 1
    }


def _effective_endpoint(endpoint):
    if endpoint.get("auth") == "default" and endpoint["protocol"] in DEFAULT_AUTH:
        return {**endpoint, "auth": DEFAULT_AUTH[endpoint["protocol"]]}
    return endpoint


def _compare_conditions(baseline, current, before, after):
    changes = []
    unknown = False
    left, right = baseline["conditions"], current["conditions"]
    revisions = left.get("provider_revision"), right.get("provider_revision")
    credentials = (
        "matching" if revisions[0] is not None and revisions[0] == revisions[1] else "unknown"
    )
    if None not in revisions and revisions[0] != revisions[1]:
        changes.append(
            {"field": "provider_revision", "baseline": revisions[0], "current": revisions[1]}
        )
    old_capability = before["probe"].get("capability", "text")
    new_capability = after["probe"].get("capability", "text")
    if old_capability != new_capability:
        changes.append(
            {"field": "probe.capability", "baseline": old_capability, "current": new_capability}
        )

    endpoint_fields = _ENDPOINT_FIELDS
    if before["protocol"] == "anthropic_messages":
        endpoint_fields += ("api_version",)
    for prefix, old_fields, new_fields, fields in (
        ("endpoint", _effective_endpoint(before), _effective_endpoint(after), endpoint_fields),
        ("probe", before["probe"], after["probe"], _PROBE_FIELDS),
        ("policy", left.get("policy", {}), right.get("policy", {}), _POLICY_FIELDS),
    ):
        for name in fields:
            old, new = old_fields.get(name, _MISSING), new_fields.get(name, _MISSING)
            if prefix == "policy" and old is new is _MISSING:
                continue
            if old is _MISSING or new is _MISSING or _redacted(old) or _redacted(new):
                unknown = True
            elif old != new:
                changes.append({"field": f"{prefix}.{name}", "baseline": old, "current": new})

    if changes:
        status = "changed"
    elif unknown or credentials == "unknown":
        status = "unknown"
    else:
        status = "matching"
    return {"status": status, "credentials": credentials, "changes": changes}, unknown


def _at_endpoint(rows, endpoint):
    return [
        row
        for row in rows
        if row["endpoint_id"] == endpoint["id"] and row["protocol"] == endpoint["protocol"]
    ]


def _catalog(snapshot, endpoint):
    records = _at_endpoint(snapshot["discovery"], endpoint)
    if not records:
        return "missing", set()
    models = [model for record in records for model in record["models"]]
    candidates = {model for model in models if _identifiable(model)}
    if all(record["source"] == "manual" for record in records):
        status = "manual"
    elif (
        len(records) == 1
        and records[0]["source"] == "api"
        and records[0]["status"] == "completed"
        and len(models) == len(candidates)
    ):
        status = "complete"
    else:
        status = "partial"
    return status, candidates


def _observations(snapshot, endpoint):
    rows = _at_endpoint(snapshot["results"], endpoint)
    counts = Counter(row["model"] for row in rows)
    ambiguous = {model for model, count in counts.items() if count > 1 or not _identifiable(model)}
    return {row["model"]: row for row in rows if row["model"] not in ambiguous}, ambiguous


def _verification(before, after, *, changed, unknown):
    old, new = before.get("status"), after.get("status")
    if old not in _TESTED or new not in _TESTED:
        return "not_tested"
    if before.get("capability", "text") != after.get("capability", "text"):
        return "not_tested"
    old_probe, new_probe = before.get("probe_id"), after.get("probe_id")
    identifiable_probes = _identifiable(old_probe) and _identifiable(new_probe)
    if changed or (identifiable_probes and old_probe != new_probe):
        return "conditions_changed"
    if unknown or not identifiable_probes:
        return "conditions_unknown"
    if old == new:
        return "unchanged"
    return "regressed" if old == "available" else "recovered"


def compare_scans(baseline: dict, current: dict, endpoint_id: str | None = None) -> dict:
    """Return saved observations for one endpoint, or common choices for endpoint selection."""
    before, after = baseline["view"], current["view"]
    if before["scan_id"] == after["scan_id"]:
        raise ComparisonError("Choose two different scans")
    if before["state"] not in _TERMINAL or after["state"] not in _TERMINAL:
        raise ComparisonError("Both scans must have ended")
    if not _identifiable(before["provider_id"]) or before["provider_id"] != after["provider_id"]:
        raise ComparisonError("Both scans must have the same identifiable provider")

    before_endpoints, after_endpoints = _endpoints(baseline), _endpoints(current)
    common = {
        key: before_endpoints[key]["protocol"]
        for key in sorted(before_endpoints.keys() & after_endpoints.keys())
        if before_endpoints[key]["protocol"] == after_endpoints[key]["protocol"]
    }
    if not common:
        raise ComparisonError("The scans share no identifiable endpoint with the same protocol")
    if endpoint_id is not None and endpoint_id not in common:
        raise ComparisonError("The selected endpoint is not shared with the same protocol")
    if endpoint_id is None and len(common) == 1:
        endpoint_id = next(iter(common))

    comparison = {
        "baseline": {key: before[key] for key in _RUN_FIELDS},
        "current": {key: after[key] for key in _RUN_FIELDS},
        "endpoints": [{"id": key, "protocol": protocol} for key, protocol in common.items()],
        "endpoint_id": endpoint_id,
        "conditions": None,
        "catalog": None,
        "rows": [],
    }
    if endpoint_id is None:
        return comparison

    left_endpoint, right_endpoint = before_endpoints[endpoint_id], after_endpoints[endpoint_id]
    left_capability = left_endpoint["probe"].get("capability", "text")
    right_capability = right_endpoint["probe"].get("capability", "text")
    if (left_capability, right_capability) != ("text", "text"):
        comparison["capabilities"] = {"baseline": left_capability, "current": right_capability}
    conditions, unknown_fields = _compare_conditions(
        baseline, current, left_endpoint, right_endpoint
    )
    left_catalog, left_models = _catalog(baseline, left_endpoint)
    right_catalog, right_models = _catalog(current, right_endpoint)
    changed = conditions["status"] == "changed"
    comparable_catalog = (
        left_catalog == right_catalog == "complete" and not changed and not unknown_fields
    )
    left_results, left_ambiguous = _observations(baseline, left_endpoint)
    right_results, right_ambiguous = _observations(current, right_endpoint)
    model_ids = (left_models | right_models | left_results.keys() | right_results.keys()) - (
        left_ambiguous | right_ambiguous
    )

    rows = []
    for model in sorted(model_ids):
        old, new = left_results.get(model, {}), right_results.get(model, {})
        catalog_change = None
        if comparable_catalog and (model in left_models) != (model in right_models):
            catalog_change = "added" if model in right_models else "removed"
        rows.append(
            {
                "model": model,
                "baseline_status": old.get("status"),
                "current_status": new.get("status"),
                "baseline_error": old.get("error_category"),
                "current_error": new.get("error_category"),
                "verification": _verification(old, new, changed=changed, unknown=unknown_fields),
                "catalog": catalog_change,
            }
        )
    comparison.update(
        conditions=conditions,
        catalog={
            "baseline": left_catalog,
            "current": right_catalog,
            "comparable": comparable_catalog,
        },
        rows=rows,
    )
    return comparison
