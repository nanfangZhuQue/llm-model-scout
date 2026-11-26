from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import re
import secrets
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, ConfigDict, Field, StringConstraints
from starlette.responses import FileResponse, JSONResponse, Response

from .. import __version__
from ..config import ConfigError
from ..domain import Capability, DiscoveryResult, ModelResult, Protocol, ScanReport
from ..providers import MANAGED_ENDPOINT_IDS, StoreConflict, StoreError
from ..report import report_csv, report_dict
from .comparison import ComparisonError, compare_scans
from .history import HistoryError
from .manager import HISTORY_UNAVAILABLE, RetestSubmission, TaskError, TaskManager
from .provider_service import ProviderService
from .retest import RetestConnectionError, RetestError, prepare_retest

STATIC_ROUTES = {
    "/": ("index.html", "text/html"),
    "/ui/app.js": ("app.js", "text/javascript"),
    "/ui/providers.js": ("providers.js", "text/javascript"),
    "/ui/app.css": ("app.css", "text/css"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; "
    "img-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
)

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,80}$")]
ModelId = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=256)]


def _local_stamp(value) -> str:
    try:
        return datetime.fromisoformat(str(value)).astimezone().strftime("%Y%m%d-%H%M%S")
    except (ValueError, TypeError):
        return datetime.now().astimezone().strftime("%Y%m%d-%H%M%S")


def download_filename(view, output_format: str, scope: str = "all") -> str:
    """XLSX uses provider name plus a second-precision local timestamp."""
    scan_id = view["scan_id"]
    partial = view.get("report_partial", False)
    fallback = f"scan-{scan_id}" + ("-partial" if partial else "")
    if output_format != "xlsx":
        return f'attachment; filename="{fallback}.{output_format}"'
    raw = (view["provider_name"] or view["provider_id"] or "scan").strip()
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]', "_", raw)[:60].rstrip(" .") or "scan"
    name = f"{safe}-{_local_stamp(view['finished_at'] or view['created_at'])}"
    if partial:
        name += "-部分"
    if scope == "passed":
        name += "-仅通过"
    name += ".xlsx"
    ascii_fallback = name if name.isascii() else f"{fallback}.xlsx"
    return f"attachment; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(name)}"


def _report_from_document(document):
    provider, scan = document["provider"], document["scan"]
    return ScanReport(
        provider["name"],
        provider["endpoints"],
        scan["policy"],
        status=scan["status"],
        reason=scan["reason"],
        started_at=scan["started_at"],
        finished_at=scan["finished_at"],
        requests_sent=scan["requests_sent"],
        discovery=[DiscoveryResult(**row) for row in document["discovery"]],
        results=[ModelResult(**row) for row in document["results"]],
    )


def _checkpoint_report(saved):
    view, conditions = saved["view"], saved["conditions"]
    report = ScanReport(
        view["provider_name"],
        [
            {key: endpoint[key] for key in ("id", "protocol", "base_url", "probe")}
            for endpoint in conditions["endpoints"]
        ],
        conditions["policy"],
        status=view["state"],
        reason="agent_interrupted" if view["state"] == "interrupted" else "checkpoint_only",
        started_at=view["started_at"] or view["created_at"],
        finished_at=view["finished_at"],
        requests_sent=None,
        discovery=[DiscoveryResult(**row) for row in saved["discovery"]],
        results=[
            ModelResult(**row)
            for row in sorted(
                saved["results"],
                key=lambda row: (row["endpoint_id"], row["model"], row["probe_id"]),
            )
        ],
    )
    data = report_dict(report)
    data["scan"]["partial"] = True
    return data


class CreateScan(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    operation: Literal["scan", "models"] = "scan"
    mode: Literal["batch", "quick"] = "batch"
    model_selections: dict[Identifier, list[ModelId]] | None = Field(
        default=None, max_length=len(MANAGED_ENDPOINT_IDS)
    )
    provider_id: Identifier
    endpoint_id: Identifier | None = None
    models: list[ModelId] | None = Field(default=None, min_length=1, max_length=1000)
    profile: Literal["baseline", "chat-basic"] | None = None
    capability: Capability | None = None
    max_tokens: int | None = Field(default=None, ge=1, le=65536)
    max_requests: int | None = Field(default=None, ge=1, le=1_000_000)
    scan_timeout: float | None = Field(default=None, gt=0, le=300, allow_inf_nan=False)


class CreateRetest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    baseline_id: Identifier
    current_id: Identifier
    endpoint_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.-]{1,2048}$")]
    models: list[Annotated[str, StringConstraints(min_length=1)]] = Field(
        min_length=1, max_length=1000
    )
    request_id: Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{16,80}$")]
    confirmation: Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")] | None = None


class EndpointInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    protocol: Protocol
    base_url: str = Field(min_length=1, max_length=2048)
    api_key: str | None = Field(default=None, max_length=4096, repr=False)
    proxy: str | None = Field(default=None, max_length=2048, repr=False)


class ProviderInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    name: str = Field(min_length=1, max_length=80)
    endpoints: list[EndpointInput] = Field(min_length=1, max_length=len(MANAGED_ENDPOINT_IDS))
    revision: int | None = Field(default=None, ge=1)


class ExportInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    include_secrets: bool = False
    confirm_plaintext: bool = False


class LocalBoundary:
    def __init__(self, app, token: str, port: int):
        self.app = app
        self.token = token.encode("ascii")
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        if port == 80:
            self.hosts.update(("127.0.0.1", "localhost"))
        self.origins = {"http://" + host for host in self.hosts}

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        started = False

        async def safe_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = dict(message)
                message["headers"] = list(message.get("headers", [])) + [
                    (b"cache-control", b"no-store"),
                    (b"x-content-type-options", b"nosniff"),
                    (b"content-security-policy", CSP.encode("ascii")),
                    (b"referrer-policy", b"no-referrer"),
                    (b"cross-origin-resource-policy", b"same-origin"),
                ]
            await send(message)

        async def reject(status, detail):
            await JSONResponse({"detail": detail}, status_code=status)(scope, receive, safe_send)

        def values(name):
            return [v for k, v in scope.get("headers", []) if k.lower() == name]

        hosts = values(b"host")
        if len(hosts) != 1 or hosts[0].decode("latin-1").lower() not in self.hosts:
            return await reject(403, "Host not allowed")
        origins = values(b"origin")
        if len(origins) > 1 or (origins and origins[0].decode("latin-1") not in self.origins):
            return await reject(403, "Origin not allowed")
        public = (scope["path"] == "/health" and scope["method"] == "GET") or (
            scope["path"] in STATIC_ROUTES and scope["method"] in ("GET", "HEAD")
        )
        if not public:
            auth = values(b"authorization")
            if (
                len(auth) != 1
                or not auth[0].startswith(b"Bearer ")
                or not secrets.compare_digest(auth[0][7:], self.token)
            ):
                return await reject(401, "Agent authentication required")
        body = bytearray()
        if scope["method"] in ("POST", "PUT"):
            lengths = values(b"content-length")
            try:
                if len(lengths) > 1 or (lengths and int(lengths[0]) < 0):
                    return await reject(400, "Invalid content length")
                if lengths and int(lengths[0]) > 65536:
                    return await reject(413, "Request body too large")
            except ValueError:
                return await reject(400, "Invalid content length")
            if values(b"content-encoding") not in ([], [b"identity"]):
                return await reject(415, "Encoded request bodies are not supported")
            if scope["path"] in ("/scans", "/scans/retest", "/scans/preview") or scope[
                "path"
            ].startswith("/providers"):
                kinds = values(b"content-type")
                if (
                    len(kinds) != 1
                    or kinds[0].split(b";")[0].strip().lower() != b"application/json"
                ):
                    return await reject(415, "Expected application/json")
            try:
                async with asyncio.timeout(5):
                    while True:
                        message = await receive()
                        if message["type"] == "http.disconnect":
                            return
                        body.extend(message.get("body", b""))
                        if len(body) > 65536:
                            return await reject(413, "Request body too large")
                        if not message.get("more_body", False):
                            break
            except TimeoutError:
                return await reject(408, "Request body timed out")

        replayed = False

        async def replay():
            nonlocal replayed
            if scope["method"] in ("POST", "PUT") and not replayed:
                replayed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, safe_send)
        except Exception:
            # Swallow framework re-raises so Uvicorn cannot log credential-bearing exceptions.
            if not started:
                await reject(500, "Internal Agent error; details withheld")


def create_app(
    registry,
    *,
    token: str,
    port=8766,
    max_running=2,
    max_queued=16,
    max_history=50,
    http_transport=None,
    provider_store=None,
    history_store=None,
) -> FastAPI:
    if not token.isascii() or len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError("Invalid Agent token")
    manager = TaskManager(
        registry,
        max_running=max_running,
        max_queued=max_queued,
        max_history=max_history,
        http_transport=http_transport,
        history_store=history_store,
    )

    service = ProviderService(manager, provider_store)
    confirmation_key = secrets.token_bytes(32)

    @asynccontextmanager
    async def lifespan(app):
        try:
            manager.start()
            yield
        finally:
            await manager.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.manager = manager
    app.add_middleware(LocalBoundary, token=token, port=port)

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request, exc):
        return JSONResponse({"detail": "Invalid request schema or parameters"}, status_code=422)

    @app.exception_handler(TaskError)
    async def task_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=exc.status)

    @app.exception_handler(HistoryError)
    async def history_error(request, exc):
        detail = (
            "Scan history could not be read" if request.method == "GET" else HISTORY_UNAVAILABLE
        )
        return JSONResponse({"detail": detail}, status_code=503)

    @app.exception_handler(StoreConflict)
    async def store_conflict(request, exc):
        return JSONResponse({"detail": "Provider changed; refresh before editing"}, status_code=409)

    @app.exception_handler(StoreError)
    async def store_error(request, exc):
        return JSONResponse(
            {"detail": "Encrypted local storage unavailable; no plaintext fallback"},
            status_code=503,
        )

    @app.exception_handler(ConfigError)
    async def provider_input_error(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=422)

    def scan_view(scan_id, saved=None):
        job = manager.jobs.get(scan_id)
        if job is None:
            if saved is None:
                raise TaskError(404, "Unknown or evicted scan")
            return saved
        live = job.view()
        if saved is None:
            return live
        if job.history_finalized and saved["finished_at"] is None:
            # This read began before finalization. Keep its state and report flags together.
            return saved
        return {
            **saved,
            **live,
            "provider_id": saved["provider_id"],
            "provider_name": saved["provider_name"],
            "report_available": saved["report_available"] or live["report_available"],
        }

    async def saved_view(scan_id):
        if history_store is not None:
            return await asyncio.to_thread(history_store.get_view, scan_id)
        return None

    def require_finished_pair(baseline, current):
        if baseline is None or current is None:
            raise HTTPException(404, "Unknown or evicted comparison scan")
        if any(
            run["view"]["state"] in ("queued", "running", "cancelling")
            for run in (baseline, current)
        ):
            raise HTTPException(409, "Wait for both scans to finish")

    @app.get("/health")
    async def health():
        return {"status": "ok", "version": __version__}

    @app.get("/providers")
    async def providers():
        await service.refresh()
        return {"providers": service.inventory(), "management_enabled": provider_store is not None}

    @app.post("/providers", status_code=201)
    async def add_provider(payload: ProviderInput):
        if payload.revision is not None:
            raise HTTPException(422, "New provider must not include a revision")
        return await service.save(payload.model_dump(exclude={"revision"}, exclude_unset=True))

    @app.put("/providers/{provider_id}")
    async def edit_provider(provider_id: Identifier, payload: ProviderInput):
        if payload.revision is None:
            raise HTTPException(422, "Revision required")
        return await service.save(
            payload.model_dump(exclude={"revision"}, exclude_unset=True),
            provider_id,
            payload.revision,
        )

    @app.delete("/providers/{provider_id}")
    async def delete_provider(provider_id: Identifier, revision: Annotated[int, Query(ge=1)]):
        await service.delete(provider_id, revision)
        return {"deleted": True}

    @app.post("/providers/{provider_id}/export")
    async def export_provider(provider_id: Identifier, payload: ExportInput):
        if payload.include_secrets and not payload.confirm_plaintext:
            raise HTTPException(422, "Explicit plaintext export confirmation is required")
        data = await service.export(provider_id, payload.include_secrets)
        suffix = ".private.json" if payload.include_secrets else ".json"
        return Response(
            json.dumps(data, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={
                "Content-Disposition": f'attachment; filename="provider-{provider_id}{suffix}"'
            },
        )

    def static_handler(filename, media_type):
        asset_path = Path(__file__).resolve().parents[1] / "web" / filename

        async def serve():
            return FileResponse(asset_path, media_type=media_type)

        return serve

    for route, (filename, media_type) in STATIC_ROUTES.items():
        app.add_api_route(
            route,
            static_handler(filename, media_type),
            methods=["GET", "HEAD"],
            include_in_schema=False,
        )

    @app.get("/scans")
    async def list_scans(
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        offset: Annotated[int, Query(ge=0, le=2**63 - 1)] = 0,
        provider_id: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    ):
        if history_store is not None:
            page = await asyncio.to_thread(
                history_store.list_views, limit=limit, offset=offset, provider_id=provider_id
            )
            page["scans"] = [scan_view(row["scan_id"], row) for row in page["scans"]]
        else:
            rows = [job.view() for job in reversed(manager.jobs.values())]
            names = {}
            for row in rows:
                names.setdefault(row["provider_id"], row["provider_name"])
            matching = [
                row for row in rows if provider_id is None or row["provider_id"] == provider_id
            ]
            page = {
                "scans": matching[offset : offset + limit],
                "total": len(matching),
                "providers": [
                    {"provider_id": key, "provider_name": name}
                    for key, name in sorted(names.items())
                ],
            }
        return {**page, "limit": limit, "offset": offset}

    @app.post("/scans/preview")
    async def preview_scan(payload: CreateScan):
        await service.refresh()
        if not service.visible(payload.provider_id):
            raise HTTPException(404, "Unknown provider")
        return manager.preview(payload)

    @app.post("/scans", status_code=202)
    async def start_scan(payload: CreateScan):
        await service.refresh()
        if not service.visible(payload.provider_id):
            raise HTTPException(404, "Unknown provider")
        revision = service.records.get(payload.provider_id, {}).get("revision")
        return manager.submit(payload, provider_revision=revision).view()

    @app.post("/scans/retest", status_code=202)
    async def start_retest(payload: CreateRetest):
        if history_store is None:
            raise HTTPException(409, "Scan history is not enabled")
        source = {
            "baseline_id": payload.baseline_id,
            "current_id": payload.current_id,
            "endpoint_id": payload.endpoint_id,
        }
        models = sorted(set(payload.models))
        fingerprint = hashlib.sha256(json.dumps([source, models]).encode()).hexdigest()

        def previous_view(previous):
            digest, saved = previous
            if digest != fingerprint:
                raise HTTPException(409, "This request ID belongs to a different retest")
            return scan_view(saved["scan_id"], saved)

        previous = await asyncio.to_thread(history_store.get_retest, payload.request_id)
        if previous is not None:
            return previous_view(previous)

        baseline, current = await asyncio.to_thread(
            history_store.get_pair, payload.baseline_id, payload.current_id
        )
        await service.refresh()
        # The Agent owns the history writer. No await between this check and submission.
        previous = history_store.get_retest(payload.request_id)
        if previous is not None:
            return previous_view(previous)
        require_finished_pair(baseline, current)
        provider_id = current["view"]["provider_id"]
        if not service.visible(provider_id):
            raise HTTPException(409, "The current provider or endpoint is no longer available")
        revision = service.records.get(provider_id, {}).get("revision")
        try:
            plan = prepare_retest(
                baseline,
                current,
                payload.endpoint_id,
                models,
                manager.registry[provider_id],
                revision,
            )
        except RetestConnectionError as exc:
            raise HTTPException(409, str(exc)) from None
        except (ComparisonError, RetestError) as exc:
            raise HTTPException(422, str(exc)) from None
        if plan.changes:
            confirmation = hmac.digest(
                confirmation_key,
                json.dumps(
                    [payload.request_id, fingerprint, revision, plan.changes], sort_keys=True
                ).encode(),
                "sha256",
            ).hex()
            if not hmac.compare_digest(payload.confirmation or "", confirmation):
                return JSONResponse(
                    {
                        "detail": "The connection changed since the source scan",
                        "code": "connection_changed",
                        "changes": plan.changes,
                        "confirmation": confirmation,
                    },
                    status_code=409,
                )

        # Keep the exact stored model IDs. The normal input model trims user-entered IDs.
        request = CreateScan(provider_id=provider_id).model_copy(
            update={
                "endpoint_id": plan.endpoint_id,
                "model_selections": {plan.endpoint_id: plan.models},
            }
        )
        retest = RetestSubmission(source, plan.probe, payload.request_id, fingerprint)
        return manager.submit(request, provider_revision=revision, retest=retest).view()

    @app.get("/scans/compare")
    async def compare_history(
        baseline_id: Annotated[str, Query(min_length=1, max_length=80)],
        current_id: Annotated[str, Query(min_length=1, max_length=80)],
        endpoint_id: Annotated[str | None, Query(min_length=1, max_length=2048)] = None,
    ):
        if baseline_id == current_id:
            raise HTTPException(422, "Choose two different scans")
        if history_store is None:
            raise HTTPException(409, "Scan history is not enabled")
        baseline, current = await asyncio.to_thread(history_store.get_pair, baseline_id, current_id)
        require_finished_pair(baseline, current)
        try:
            return compare_scans(baseline, current, endpoint_id)
        except ComparisonError as exc:
            raise HTTPException(422, str(exc)) from None

    @app.get("/scans/{scan_id}")
    async def get_scan(scan_id: str):
        return scan_view(scan_id, await saved_view(scan_id))

    @app.post("/scans/{scan_id}/cancel")
    async def cancel_scan(scan_id: str):
        if scan_id in manager.jobs:
            job = manager.cancel(scan_id)
            saved = await saved_view(scan_id)
            # Retention can evict an older run during cancellation.
            if saved is None:
                return job.view()
            return scan_view(scan_id, saved)
        return scan_view(scan_id, await saved_view(scan_id))

    @app.get("/scans/{scan_id}/report")
    async def get_report(
        scan_id: str,
        output_format: Annotated[Literal["json", "csv", "xlsx"], Query(alias="format")] = "json",
        scope: Annotated[Literal["all", "passed"], Query()] = "all",
    ):
        saved = (
            await asyncio.to_thread(history_store.get, scan_id)
            if history_store is not None
            else None
        )
        view = scan_view(scan_id, saved["view"] if saved is not None else None)
        job = manager.jobs.get(scan_id)
        document = saved["report"] if saved is not None else None
        if document is None and saved is not None and view.get("report_partial"):
            document = _checkpoint_report(saved)
        if document is None and job is not None and job.report is not None:
            document = report_dict(job.report)
        if document is None:
            raise HTTPException(409, "Report is not available")
        if output_format in ("csv", "xlsx") and view["operation"] == "models":
            raise HTTPException(422, "Model discovery reports use JSON")
        if scope != "all" and output_format != "xlsx":
            raise HTTPException(422, "Selective scope is only available for XLSX downloads")
        if output_format == "xlsx":
            from ..report import report_xlsx

            report = _report_from_document(document)
            if scope == "passed":
                passed = [r for r in report.results if r.status == "available"]
                report = replace(report, results=passed)
            content = report_xlsx(report)
            media = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        elif output_format == "csv":
            content = report_csv(
                _report_from_document(document), tool_version=document["tool"]["version"]
            ).encode("utf-8-sig")
            media = "text/csv"
        else:
            if view.get("source"):
                document["scan"]["source"] = view["source"]
            content = json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False).encode(
                "utf-8"
            )
            media = "application/json"
        filename = download_filename(view, output_format, scope)
        return Response(
            content,
            media_type=media,
            headers={"Content-Disposition": filename},
        )

    return app
