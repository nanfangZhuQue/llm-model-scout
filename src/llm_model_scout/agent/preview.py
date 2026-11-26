"""Compute request bounds from a resolved scan plan."""

from ..domain import ProviderConfig


def scan_preview(
    provider: ProviderConfig, *, mode: str, operation: str, requests_remaining: int
) -> dict:
    """Describe a scan before scheduling it or acquiring any request budget."""
    policy = provider.policy
    endpoints = []
    manual_targets = 0
    discovery_endpoints = 0
    for endpoint in provider.endpoints:
        if endpoint.models is None:
            count = None
            source = "api"
            discovery_endpoints += 1
        else:
            count = len(set(endpoint.models))
            source = "manual"
            manual_targets += count
        endpoints.append(
            {
                "id": endpoint.id,
                "protocol": endpoint.protocol,
                "max_tokens": endpoint.probe.max_tokens,
                "target_count": count,
                "source": source,
            }
        )

    if operation == "models":
        target_count = 0
    elif discovery_endpoints:
        target_count = None
    else:
        target_count = min(manual_targets, policy.max_models)

    requests_remaining = max(0, requests_remaining)
    effective_max_requests = min(policy.max_requests, requests_remaining)
    upper_targets = policy.max_models if target_count is None else target_count
    upper_pages = discovery_endpoints * policy.max_pages
    request_upper_bound = min(
        effective_max_requests, (upper_pages + upper_targets) * (1 + policy.retries)
    )
    preview = {
        "mode": mode,
        "operation": operation,
        "endpoints": endpoints,
        "target_count": target_count,
        "max_models": policy.max_models,
        "max_requests": policy.max_requests,
        "requests_remaining": requests_remaining,
        "effective_max_requests": effective_max_requests,
        "retries": policy.retries,
        "scan_timeout": policy.scan_timeout,
        "timeout": policy.timeout,
        "request_upper_bound": request_upper_bound,
    }
    if operation == "scan":
        preview["capability"] = provider.endpoints[0].probe.capability
    return preview
