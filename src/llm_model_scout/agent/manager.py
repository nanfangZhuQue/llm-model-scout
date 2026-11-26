from __future__ import annotations

import asyncio
import uuid
from collections import Counter, OrderedDict
from dataclasses import asdict, dataclass, field, replace

from ..core.redact import Redactor
from ..core.scanner import scan
from ..domain import Capability, ProbeConfig, ScanReport, utc_now
from ..report import report_dict
from ..transport import RequestGate
from .history import HistoryError
from .preview import scan_preview

TERMINAL = {"completed", "failed", "cancelled"}
HISTORY_UNAVAILABLE = "Scan history could not be saved"


def redact_fields(data, redactor, *fields):
    return {**data, **{key: redactor.clean(data[key]) for key in fields}}


def history_conditions(provider, revision, redactor):
    return {
        "provider_revision": revision,
        "policy": asdict(provider.policy),
        "endpoints": [
            {
                "id": redactor.text(endpoint.id),
                "protocol": endpoint.protocol,
                "base_url": redactor.text(endpoint.base_url),
                "auth": endpoint.auth,
                "api_version": endpoint.api_version,
                "probe": redact_fields(endpoint.probe.record(), redactor, "profile_id", "prompt"),
                "models": redactor.clean(list(endpoint.models))
                if endpoint.models is not None
                else None,
                "uses_proxy": endpoint.proxy is not None,
            }
            for endpoint in provider.endpoints
        ],
    }


def history_report(report, redactor):
    data = report_dict(report)
    provider = data["provider"]
    provider["name"] = redactor.text(provider["name"])
    provider["endpoints"] = [
        {
            **redact_fields(endpoint, redactor, "id", "base_url"),
            "probe": redact_fields(endpoint["probe"], redactor, "profile_id", "prompt"),
        }
        for endpoint in provider["endpoints"]
    ]
    data["discovery"] = [
        redact_fields(row, redactor, "endpoint_id", "models", "error_message")
        for row in data["discovery"]
    ]
    data["results"] = [
        redact_fields(row, redactor, "endpoint_id", "model", "probe_id", "error_message")
        for row in data["results"]
    ]
    return data


class TaskError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class RetestSubmission:
    source: dict[str, str]
    probe: ProbeConfig
    request_id: str
    fingerprint: str


@dataclass
class Job:
    id: str
    provider_id: str
    state: str = "queued"
    created_at: str = field(default_factory=utc_now)
    started_at: str | None = None
    finished_at: str | None = None
    progress: dict = field(default_factory=lambda: {"discovered": 0, "finished": 0})
    error: str | None = None
    report: ScanReport | None = field(default=None, repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    operation: str = "scan"
    mode: str = "batch"
    capability: Capability = "text"
    display_name: str = ""
    public_provider_id: str | None = None
    history_names: tuple[str, str] | None = field(default=None, repr=False)
    source: dict[str, str] | None = None
    history_finalized: bool = field(default=False, init=False, repr=False)

    def view(self):
        view = {
            "scan_id": self.id,
            "provider_id": self.public_provider_id or self.provider_id,
            "provider_name": self.display_name,
            "operation": self.operation,
            "state": self.state,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "progress": dict(self.progress),
            "error": self.error,
            "report_available": self.report is not None,
        }
        if self.source is not None:
            view["source"] = dict(self.source)
        if self.mode == "quick":
            view["mode"] = self.mode
        if self.capability != "text":
            view["capability"] = self.capability
        return view

    def history_view(self):
        provider_id, provider_name = self.history_names
        return {**self.view(), "provider_id": provider_id, "provider_name": provider_name}


class TaskManager:
    def __init__(
        self,
        registry,
        *,
        max_running=2,
        max_queued=16,
        max_history=50,
        http_transport=None,
        history_store=None,
    ):
        if min(max_running, max_queued, max_history) < 1:
            raise ValueError("Agent task limits must be positive")
        self.registry = registry
        self.max_running = max_running
        self.max_queued = max_queued
        self.max_history = max_history
        self.slots = asyncio.Semaphore(max_running)
        self.gates = {name: RequestGate(provider.policy) for name, provider in registry.items()}
        self.jobs: OrderedDict[str, Job] = OrderedDict()
        self.tasks = set()
        self.http_transport = http_transport
        self.history_store = history_store
        self.closing = False

    def start(self):
        if self.history_store is not None:
            self.history_store.open()
            self.history_store.recover(utc_now(), max_history=self.max_history)

    def inventory(self):
        return [
            Redactor.for_provider(provider).clean(
                {
                    "provider_id": name,
                    "endpoints": [
                        {
                            "id": e.id,
                            "protocol": e.protocol,
                            "probe": {
                                "profile_id": e.probe.profile_id,
                                "max_tokens": e.probe.max_tokens,
                                "capability": e.probe.capability,
                            },
                            "has_manual_models": e.models is not None,
                        }
                        for e in provider.endpoints
                    ],
                    "limits": {
                        "max_models": min(1000, provider.policy.max_models),
                        "max_tokens": 65536,
                        "scan_timeout": min(provider.policy.scan_timeout or 300, 300),
                    },
                    "max_requests": provider.policy.max_requests,
                    "requests_remaining": max(
                        0, provider.policy.max_requests - self.gates[name].sent
                    ),
                }
            )
            for name, provider in self.registry.items()
        ]

    def _provider(self, request, *, probe_override=None):
        provider = self.registry.get(request.provider_id)
        if provider is None:
            raise TaskError(404, "Unknown provider")
        endpoints = provider.endpoints
        if request.endpoint_id != "all":
            endpoints = tuple(
                e for e in endpoints if request.endpoint_id is None or e.id == request.endpoint_id
            )
            if len(endpoints) != 1:
                raise TaskError(422, "Select one configured endpoint or all")
        selections = getattr(request, "model_selections", None)
        operation = getattr(request, "operation", "scan")
        capability = getattr(request, "capability", None)
        quick = getattr(request, "mode", "batch") == "quick"
        if quick and (
            operation != "scan"
            or request.endpoint_id == "all"
            or request.models is None
            or len(request.models) != 1
            or selections is not None
            or request.max_requests not in (None, 1)
        ):
            raise TaskError(422, "Quick verification needs one explicit model and one endpoint")
        if operation == "models" and (request.models is not None or selections is not None):
            raise TaskError(422, "Model discovery does not accept manual selections")
        if operation == "models" and capability not in (None, "text"):
            raise TaskError(422, "Model discovery does not use a verification capability")
        if selections is not None:
            if (
                request.models is not None
                or not selections
                or not set(selections) <= {e.id for e in endpoints}
            ):
                raise TaskError(422, "Invalid per-endpoint model selection")
            if sum(len(set(items)) for items in selections.values()) > provider.policy.max_models:
                raise TaskError(422, "Model selection exceeds server limit")
            endpoints = tuple(e for e in endpoints if e.id in selections)
        models = None if request.models is None else tuple(dict.fromkeys(request.models))
        if models is not None and len(models) > provider.policy.max_models:
            raise TaskError(422, "Model selection exceeds server limit")
        selected = []
        for endpoint in endpoints:
            probe = probe_override or (
                endpoint.probe if request.profile is None else ProbeConfig.named(request.profile)
            )
            if probe_override is None:
                effective_capability = capability or endpoint.probe.capability
                probe = replace(
                    probe, capability="text" if operation == "models" else effective_capability
                )
            if request.max_tokens is not None:
                probe = replace(probe, max_tokens=request.max_tokens)
            chosen = endpoint.models if models is None else models
            if selections is not None:
                chosen = tuple(dict.fromkeys(selections[endpoint.id]))
                if not chosen:
                    raise TaskError(422, "Selected model list must not be empty")
            if operation == "models":
                chosen = None
            selected.append(replace(endpoint, probe=probe, models=chosen))
        if len({endpoint.probe.capability for endpoint in selected}) != 1:
            raise TaskError(422, "Select one verification capability for the task")
        max_requests = 1 if quick else request.max_requests or provider.policy.max_requests
        if max_requests > provider.policy.max_requests:
            raise TaskError(422, "Request budget exceeds server limit")
        deadline = min(provider.policy.scan_timeout or 300, 300)
        if request.scan_timeout is not None:
            if request.scan_timeout > deadline:
                raise TaskError(422, "Task timeout exceeds server limit")
            deadline = request.scan_timeout
        policy = replace(
            provider.policy,
            max_requests=max_requests,
            scan_timeout=deadline,
            retries=0 if quick else provider.policy.retries,
        )
        return replace(provider, endpoints=tuple(selected), policy=policy)

    def preview(self, request) -> dict:
        provider = self._provider(request)
        gate = self.gates[request.provider_id]
        preview = scan_preview(
            provider,
            mode=request.mode,
            operation=request.operation,
            requests_remaining=max(0, gate.policy.max_requests - gate.sent),
        )
        redactor = Redactor.for_provider(
            self.registry[request.provider_id], include_short_proxy_secrets=True
        )
        for endpoint in preview["endpoints"]:
            endpoint["id"] = redactor.text(endpoint["id"])
        return preview

    def submit(
        self, request, *, provider_revision=None, retest: RetestSubmission | None = None
    ) -> Job:
        if self.closing:
            raise TaskError(503, "Agent is shutting down")
        provider = self._provider(request, probe_override=retest.probe if retest else None)
        active = sum(job.state not in TERMINAL for job in self.jobs.values())
        if active >= self.max_running + self.max_queued:
            raise TaskError(429, "Agent task capacity reached")
        self._prune()
        redactor = Redactor.for_provider(provider)
        job = Job(
            uuid.uuid4().hex,
            request.provider_id,
            operation=getattr(request, "operation", "scan"),
            mode=getattr(request, "mode", "batch"),
            capability=provider.endpoints[0].probe.capability,
            display_name=redactor.text(provider.name),
            public_provider_id=redactor.text(request.provider_id),
            source=retest.source if retest else None,
        )
        history_redactor = None
        if self.history_store is not None:
            history_redactor = Redactor.for_provider(
                self.registry[request.provider_id], include_short_proxy_secrets=True
            )
            job.history_names = (
                history_redactor.text(request.provider_id),
                history_redactor.text(provider.name),
            )
            if job.source is not None:
                job.source = {
                    **job.source,
                    "endpoint_id": history_redactor.text(job.source["endpoint_id"]),
                }
            conditions = history_conditions(provider, provider_revision, history_redactor)
            if retest is None:
                self.history_store.create(job.history_view(), conditions)
            else:
                self.history_store.create(
                    job.history_view(),
                    conditions,
                    retest_key=(retest.request_id, retest.fingerprint),
                )
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run(job, provider, history_redactor))
        self.tasks.add(job.task)
        job.task.add_done_callback(self.tasks.discard)
        return job

    def get(self, scan_id: str) -> Job:
        job = self.jobs.get(scan_id)
        if job is None:
            raise TaskError(404, "Unknown or evicted scan")
        return job

    def cancel(self, scan_id: str) -> Job:
        job = self.get(scan_id)
        if job.state in TERMINAL or job.state == "cancelling":
            return job
        if job.state == "queued":
            job.state = "cancelled"
            job.finished_at = utc_now()
            self._finish_history(job)
        else:
            job.state = "cancelling"
        if job.task:
            job.task.cancel()
        self._prune()
        return job

    def _prune(self):
        ended = sorted(
            (j for j in self.jobs.values() if j.state in TERMINAL),
            key=lambda j: j.finished_at or j.created_at,
        )
        for job in ended[: max(0, len(ended) - self.max_history)]:
            self.jobs.pop(job.id, None)

    def _finish_history(self, job, redactor=None):
        if self.history_store is None:
            return
        try:
            report = history_report(job.report, redactor) if job.report is not None else None
            evicted = self.history_store.finish(job.history_view(), report)
            job.history_finalized = True
            for scan_id in evicted or ():
                self.jobs.pop(scan_id, None)
        except HistoryError:
            job.error = HISTORY_UNAVAILABLE
            if job.state == "completed":
                job.state = "failed"
                job.report = replace(job.report, status="failed", reason="history_write_failed")

    async def _run(self, job, provider, history_redactor):
        def progress(event):
            counts = dict(job.progress)
            records = {}
            if event["event"] == "discovered":
                counts["discovered"] += event["count"]
                if self.history_store is not None:
                    discovery = {k: v for k, v in event.items() if k not in ("event", "count")}
                    records["discovery"] = redact_fields(
                        discovery, history_redactor, "endpoint_id", "models", "error_message"
                    )
            elif event["event"] == "planned":
                counts.update(planned=event["count"], skipped=0, cancelled=0)
            elif event["event"] == "result":
                status = event["status"]
                count = "finished" if status in ("available", "unavailable") else status
                counts[count] += 1
                if self.history_store is not None:
                    result = {k: v for k, v in event.items() if k != "event"}
                    records["result"] = redact_fields(
                        result,
                        history_redactor,
                        "endpoint_id",
                        "model",
                        "probe_id",
                        "error_message",
                    )
            if self.history_store is not None:
                self.history_store.checkpoint({**job.history_view(), "progress": counts}, **records)
            job.progress = counts

        try:
            async with self.slots:
                if job.state == "cancelled":
                    return
                job.state = "running"
                job.started_at = utc_now()
                if self.history_store is not None:
                    self.history_store.checkpoint(job.history_view())
                job.report = await scan(
                    provider,
                    discover_only=job.operation == "models",
                    on_event=progress,
                    shared_gate=self.gates[job.provider_id],
                    http_transport=self.http_transport,
                )
                job.state = job.report.status
                if job.state == "failed":
                    job.error = job.report.reason or "Scan did not complete"
                job.progress["total"] = len(job.report.results)
                if "planned" in job.progress:
                    # Unstarted and interrupted targets may have no live result event.
                    # Rows excluded by the model limit remain in the report for compatibility.
                    counts = Counter(
                        r.status for r in job.report.results if r.reason != "model_budget_exhausted"
                    )
                    job.progress.update(
                        finished=counts["available"] + counts["unavailable"],
                        skipped=counts["skipped"],
                        cancelled=counts["cancelled"],
                    )
        except asyncio.CancelledError:
            job.state = "cancelled"
        except HistoryError:
            job.state = "failed"
            job.error = HISTORY_UNAVAILABLE
        except Exception:
            job.state = "failed"
            job.error = "Scan failed internally; credentials and exception details withheld"
        finally:
            if job.finished_at is None:
                job.finished_at = utc_now()
                self._finish_history(job, history_redactor)
            self._prune()

    async def close(self):
        self.closing = True
        # Do not cancel a task a second time while it is collecting its partial report.
        try:
            for job in list(self.jobs.values()):
                if job.state not in TERMINAL:
                    self.cancel(job.id)
            if self.tasks:
                await asyncio.gather(*list(self.tasks), return_exceptions=True)
        finally:
            if self.history_store is not None:
                self.history_store.close()
