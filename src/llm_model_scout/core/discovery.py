from ..adapters import InvalidResponse, models_page
from ..domain import DiscoveryResult, EndpointConfig, ErrorCategory
from ..transport import BudgetExhausted, Transport


async def list_models(endpoint: EndpointConfig, transport: Transport) -> DiscoveryResult:
    result = DiscoveryResult(
        endpoint.id, endpoint.protocol, "manual" if endpoint.models is not None else "api"
    )
    if endpoint.models is not None:
        result.models = sorted(set(endpoint.models))
        if not result.models:
            result.status = "failed"
            result.error_message = "Manual candidate list is empty"
        return result
    seen_cursors = set()
    ids = set()
    params = None
    for _ in range(transport.policy.max_pages):
        try:
            outcome = await transport.exchange(
                endpoint, "GET", "models", params=params, stage="discovery"
            )
        except BudgetExhausted:
            result.error_message = "request_budget_exhausted"
            break
        result.http_status = outcome.http_status
        if outcome.category:
            result.error_category = outcome.category
            result.error_message = outcome.message
            break
        try:
            page_ids, cursor = models_page(outcome.data)
        except InvalidResponse as exc:
            result.error_category = ErrorCategory.BAD_RESPONSE
            result.error_message = str(exc)
            break
        ids.update(page_ids)
        result.pages += 1
        if len(ids) > transport.policy.max_models:
            result.error_message = "Discovery truncated at max_models; more candidates may exist"
            ids = set(sorted(ids)[: transport.policy.max_models])
            break
        if cursor is None:
            result.models = sorted(ids)
            if not result.models:
                result.status = "failed"
                result.error_message = "Model discovery returned no candidates; use a manual list"
            return result
        if cursor in seen_cursors:
            result.error_category = ErrorCategory.BAD_RESPONSE
            result.error_message = "Repeated pagination cursor"
            break
        seen_cursors.add(cursor)
        params = (
            {"after_id": cursor} if endpoint.protocol == "anthropic_messages" else {"after": cursor}
        )
    else:
        result.error_message = "Discovery stopped at max_pages"
    result.models = sorted(ids)
    result.status = "partial" if ids else "failed"
    return result
