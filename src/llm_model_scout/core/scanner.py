from __future__ import annotations

import asyncio
import inspect
import re
from dataclasses import asdict

from ..domain import DiscoveryResult, ModelResult, ProviderConfig, ScanReport, utc_now
from ..transport import Transport
from .discovery import list_models
from .redact import Redactor
from .verifier import verify_model


async def scan(
    provider: ProviderConfig,
    *,
    discover_only=False,
    include: str | None = None,
    exclude: str | None = None,
    on_event=None,
    http_transport=None,
    shared_gate=None,
) -> ScanReport:
    """Return a partial report on cancellation; unexpected programming failures still propagate."""
    include_re = re.compile(include) if include else None
    exclude_re = re.compile(exclude) if exclude else None
    redactor = Redactor.for_provider(provider)
    report = ScanReport(
        provider.name,
        [
            {"id": e.id, "protocol": e.protocol, "base_url": e.base_url, "probe": e.probe.record()}
            for e in provider.endpoints
        ],
        asdict(provider.policy),
    )
    targets = []
    current_endpoint = None
    workers = []

    async def emit(event):
        if on_event:
            public_event = redactor.clean(event)
            # Event kinds and result statuses are fixed values used by progress consumers.
            public_event["event"] = event["event"]
            if event["event"] in ("discovered", "result"):
                for key in (
                    "protocol",
                    "status",
                    "source",
                    "error_category",
                    "reason",
                    "capability",
                ):
                    if key in event:
                        public_event[key] = event[key]
            returned = on_event(public_event)
            if inspect.isawaitable(returned):
                await returned

    async with Transport(provider, redactor, http_transport, shared_gate) as transport:

        async def execute():
            nonlocal current_endpoint
            for endpoint in provider.endpoints:
                current_endpoint = endpoint
                discovered = await list_models(endpoint, transport)
                report.discovery.append(discovered)
                current_endpoint = None
                await emit(
                    {
                        "event": "discovered",
                        **asdict(discovered),
                        "count": len(discovered.models),
                    }
                )
                if discovered.status != "completed":
                    report.status = "failed"
                    report.reason = "discovery_incomplete"
                if discover_only:
                    continue
                for model in discovered.models:
                    if include_re and not include_re.search(model):
                        continue
                    if exclude_re and exclude_re.search(model):
                        continue
                    result = ModelResult(
                        endpoint.id,
                        endpoint.protocol,
                        model,
                        endpoint.probe.profile_id,
                        capability=endpoint.probe.capability,
                    )
                    report.results.append(result)
                    if len(targets) >= provider.policy.max_models:
                        result.reason = "model_budget_exhausted"
                    else:
                        targets.append((endpoint, result))
            if discover_only:
                return
            await emit({"event": "planned", "count": len(targets)})
            queue = asyncio.Queue(maxsize=provider.policy.concurrency * 2)

            async def worker():
                while True:
                    item = await queue.get()
                    try:
                        if item is None:
                            return
                        endpoint, result = item
                        await verify_model(endpoint, result.model, transport, result)
                        await emit({"event": "result", **result.record()})
                    finally:
                        queue.task_done()

            for _ in range(min(provider.policy.concurrency, len(targets))):
                workers.append(asyncio.create_task(worker()))
            if not workers:
                report.status = "failed"
                report.reason = "no_candidates_after_filtering"
                return

            # Monitor producer and workers together so a failed worker cannot deadlock a full queue.
            worker_count = len(workers)

            async def produce():
                for target in targets:
                    await queue.put(target)
                for _ in range(worker_count):
                    await queue.put(None)

            producer = asyncio.create_task(produce())
            workers.append(producer)
            try:
                await asyncio.gather(*workers)
            finally:
                for task in workers:
                    task.cancel()
                await asyncio.gather(*workers, return_exceptions=True)

        try:
            async with asyncio.timeout(provider.policy.scan_timeout):
                await execute()
        except TimeoutError:
            report.status = "failed"
            report.reason = "scan_deadline_exceeded"
        except asyncio.CancelledError:
            report.status = "cancelled"
            report.reason = "user_cancelled"
        finally:
            if current_endpoint:
                report.discovery.append(
                    DiscoveryResult(
                        current_endpoint.id,
                        current_endpoint.protocol,
                        "api",
                        "cancelled",
                        error_message=report.reason or "Discovery interrupted",
                    )
                )
            for result in report.results:
                if result.reason == "not_started":
                    result.reason = report.reason or "not_executed"
            if report.status == "completed" and any(
                r.status == "skipped" or r.reason == "retry_budget_exhausted"
                for r in report.results
            ):
                report.status = "failed"
                report.reason = "scan_incomplete"
            report.requests_sent = transport.gate.sent
            report.finished_at = utc_now()
    report.results.sort(key=lambda r: (r.endpoint_id, r.model, r.probe_id))
    # Do not return provider credentials; scrub every free-text field before exposing to callers.
    for discovery in report.discovery:
        discovery.models = [redactor.text(m) for m in discovery.models]
        discovery.error_message = redactor.text(discovery.error_message, 400)
        discovery.endpoint_id = redactor.text(discovery.endpoint_id)
    for result in report.results:
        result.model = redactor.text(result.model)
        result.endpoint_id = redactor.text(result.endpoint_id)
        result.error_message = redactor.text(result.error_message, 400)
    report.provider = redactor.text(report.provider)
    report.endpoints = redactor.clean(report.endpoints)
    for endpoint, config in zip(report.endpoints, provider.endpoints, strict=True):
        endpoint["probe"]["capability"] = config.probe.capability
    return report
