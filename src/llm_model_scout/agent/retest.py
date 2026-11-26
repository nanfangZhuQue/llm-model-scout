"""Validate retest targets and identify changes to the live connection."""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass

from ..config import ConfigError, normalize_url
from ..core.redact import Redactor
from ..domain import DEFAULT_AUTH, EndpointConfig, ProbeConfig, ProviderConfig
from .comparison import compare_scans

_REDACTION = "***REDACTED***"


class RetestError(ValueError):
    """The requested targets or stored probe cannot be used for a retest."""


class RetestConnectionError(RetestError):
    """The current registry no longer has a compatible live endpoint."""


@dataclass(frozen=True)
class RetestPlan:
    endpoint_id: str
    models: tuple[str, ...]
    probe: ProbeConfig
    changes: list[dict]


def _saved_probe(endpoint: dict) -> ProbeConfig:
    try:
        probe = endpoint["probe"]
        profile_id = probe["profile_id"]
        ProbeConfig.named(profile_id)
        temperature = probe["temperature"]
        prompt = probe["prompt"]
        if temperature is not None and (
            type(temperature) not in (int, float) or not math.isfinite(temperature)
        ):
            raise ValueError
        if not isinstance(prompt, str) or not prompt.strip() or _REDACTION in prompt:
            raise ValueError
        return ProbeConfig(
            profile_id, probe["max_tokens"], temperature, prompt, probe.get("capability", "text")
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        raise RetestError("The saved probe parameters cannot be reused") from None


def _public_url(url, redactor: Redactor) -> str | None:
    if not isinstance(url, str) or _REDACTION in url:
        return None
    try:
        normalized = normalize_url(url)
    except ConfigError:
        return None
    return normalized if redactor.text(normalized) == normalized else None


def _public_auth(auth, protocol: str, redactor: Redactor) -> str | None:
    if auth == "default":
        auth = DEFAULT_AUTH.get(protocol)
    return auth if auth in ("bearer", "x-api-key") and redactor.text(auth) == auth else None


def _public_api_version(version, redactor: Redactor) -> str | None:
    if (
        isinstance(version, str)
        and re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", version)
        and redactor.text(version) == version
    ):
        return version
    return None


def _connection_changes(
    current: dict,
    historical_endpoint: dict,
    live: EndpointConfig,
    provider: ProviderConfig,
    provider_revision: int | None,
) -> list[dict]:
    redactor = Redactor.for_provider(provider, include_short_proxy_secrets=True)
    previous_proxy = historical_endpoint.get("uses_proxy")
    fields = [
        (
            "endpoint.base_url",
            _public_url(historical_endpoint.get("base_url"), redactor),
            _public_url(live.base_url, redactor),
        ),
        (
            "endpoint.auth",
            _public_auth(historical_endpoint.get("auth"), live.protocol, redactor),
            _public_auth(live.auth, live.protocol, redactor),
        ),
        (
            "endpoint.uses_proxy",
            previous_proxy if type(previous_proxy) is bool else None,
            live.proxy is not None,
        ),
    ]
    if live.protocol == "anthropic_messages":
        fields.append(
            (
                "endpoint.api_version",
                _public_api_version(historical_endpoint.get("api_version"), redactor),
                _public_api_version(live.api_version, redactor),
            )
        )

    changes = [
        {"field": field, "baseline": previous, "current": actual}
        for field, previous, actual in fields
        if previous is None or actual is None or previous != actual
    ]
    saved_revision = current["conditions"].get("provider_revision")
    if saved_revision != provider_revision or (
        saved_revision is not None and type(saved_revision) is not int
    ):
        changes.append(
            {
                "field": "provider_revision",
                "baseline": saved_revision if type(saved_revision) is int else None,
                "current": provider_revision,
            }
        )
    return changes


def prepare_retest(
    baseline: dict,
    current: dict,
    endpoint_id: str,
    models: Iterable[str],
    provider: ProviderConfig,
    provider_revision: int | None,
) -> RetestPlan:
    """Select explicit comparison targets with the current snapshot's probe parameters."""
    comparison = compare_scans(baseline, current, endpoint_id)
    eligible = {
        row["model"]
        for row in comparison["rows"]
        if row["verification"] != "unchanged" or row["catalog"] is not None
    }
    chosen = tuple(dict.fromkeys(models))
    if not chosen or not eligible.issuperset(chosen):
        raise RetestError("Choose only changed or untested models from this comparison")

    previous = next(
        endpoint for endpoint in current["conditions"]["endpoints"] if endpoint["id"] == endpoint_id
    )
    live = [endpoint for endpoint in provider.endpoints if endpoint.id == endpoint_id]
    if len(live) != 1 or live[0].protocol != previous["protocol"]:
        raise RetestConnectionError(
            "The selected endpoint is no longer available with this protocol"
        )
    return RetestPlan(
        endpoint_id=endpoint_id,
        models=chosen,
        probe=_saved_probe(previous),
        changes=_connection_changes(current, previous, live[0], provider, provider_revision),
    )
