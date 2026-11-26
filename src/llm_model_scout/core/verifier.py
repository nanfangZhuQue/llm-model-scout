import asyncio
import json
import time

from ..adapters import InvalidResponse, adapter_for, structured, tool_call
from ..adapters.json_values import load_probe_json
from ..domain import EndpointConfig, ErrorCategory, ModelResult, utc_now
from ..transport import BudgetExhausted, Transport

_CAPABILITY_ADAPTERS = {"structured": structured, "tool": tool_call}


async def verify_model(
    endpoint: EndpointConfig,
    model_id: str,
    transport: Transport,
    result: ModelResult | None = None,
) -> ModelResult:
    result = result or ModelResult(
        endpoint.id,
        endpoint.protocol,
        model_id,
        endpoint.probe.profile_id,
        capability=endpoint.probe.capability,
    )
    adapter = adapter_for(endpoint)
    streaming = endpoint.probe.capability == "stream"
    tool_probe = endpoint.probe.capability == "tool"
    capability_adapter = _CAPABILITY_ADAPTERS.get(endpoint.probe.capability)
    body = (
        capability_adapter.request_body(endpoint, model_id)
        if capability_adapter is not None
        else adapter.request_body(model_id, endpoint.probe)
    )
    if streaming:
        body["stream"] = True
    started = time.perf_counter()

    def attempted(count):
        result.attempts = count
        result.reason = "in_flight"

    try:
        outcome = await transport.exchange(
            endpoint,
            "POST",
            adapter.PATH,
            body=body,
            on_attempt=attempted,
            stream=streaming,
            json_decoder=load_probe_json if tool_probe else json.loads,
        )
    except BudgetExhausted:
        result.status = "skipped"
        result.reason = "request_budget_exhausted"
        return result
    except asyncio.CancelledError:
        result.status = "cancelled" if result.attempts else "skipped"
        result.reason = "scan_cancelled" if result.attempts else "cancelled_before_request"
        result.elapsed_ms = (time.perf_counter() - started) * 1000
        raise
    result.timestamp = utc_now()
    result.http_status = outcome.http_status
    result.attempts = outcome.attempts
    result.latency_ms = outcome.latency_ms
    result.elapsed_ms = outcome.elapsed_ms
    result.error_category = outcome.category
    result.error_message = outcome.message
    if (
        tool_probe
        and outcome.http_status not in (None, 200)
        and outcome.category not in (ErrorCategory.TIMEOUT, ErrorCategory.NETWORK_ERROR)
    ):
        result.error_message = f"Provider returned HTTP {outcome.http_status} for the tool probe"
    if streaming:
        result.stop_reason = outcome.stop_reason
        result.diagnostic_code = outcome.diagnostic_code
        result.suggestion = outcome.suggestion
    elif outcome.category == ErrorCategory.BAD_RESPONSE:
        invalid = InvalidResponse("Response could not be parsed or exceeded the size limit")
        result.diagnostic_code = invalid.code
        result.suggestion = invalid.suggestion
    if outcome.category is None and not streaming:
        try:
            result.stop_reason = (
                capability_adapter.validate_output(endpoint.protocol, outcome.data)
                if capability_adapter is not None
                else adapter.validate_text(outcome.data)
            )
        except InvalidResponse as exc:
            result.error_category = ErrorCategory.BAD_RESPONSE
            result.error_message = str(exc)
            result.diagnostic_code = exc.code
            result.suggestion = exc.suggestion
            result.stop_reason = exc.stop_reason
    result.status = "available" if result.error_category is None else "unavailable"
    result.error_category = result.error_category or ErrorCategory.OK
    result.reason = "probe_passed" if result.status == "available" else "probe_failed"
    if outcome.retry_budget_exhausted:
        result.reason = "retry_budget_exhausted"
    return result
