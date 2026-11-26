from __future__ import annotations

import asyncio
import json
import random
import ssl
import time
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from .adapters import SUGGESTIONS, InvalidResponse, headers
from .adapters.streaming import validate_stream
from .core.classify import classify_error, error_message
from .core.redact import Redactor
from .domain import EndpointConfig, ErrorCategory, HttpOutcome, ProviderConfig, ScanPolicy


class BudgetExhausted(Exception):
    pass


def retry_after_seconds(value: str | None) -> float:
    if not value:
        return 0
    try:
        number = float(value)
        return max(0, number) if number < float("inf") else 0
    except ValueError:
        try:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                date = date.replace(tzinfo=UTC)
            return max(0, (date - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return 0


class RequestGate:
    def __init__(self, policy: ScanPolicy):
        self.policy = policy
        self.semaphore = asyncio.Semaphore(policy.concurrency)
        self.lock = asyncio.Lock()
        self.sent = 0
        self.next_start = 0.0
        self.cool_until = 0.0

    def cooldown(self, seconds: float):
        self.cool_until = max(self.cool_until, time.monotonic() + seconds)

    async def admit(self, budget: RequestGate | None = None):
        async with self.lock:
            while True:
                if self.sent >= self.policy.max_requests or (
                    budget is not None and budget.sent >= budget.policy.max_requests
                ):
                    raise BudgetExhausted("request_budget_exhausted")
                delay = max(self.next_start, self.cool_until) - time.monotonic()
                if delay <= 0:
                    break
                await asyncio.sleep(delay)
            self.sent += 1
            if budget is not None and budget is not self:
                budget.sent += 1
            if self.policy.rate_limit:
                self.next_start = time.monotonic() + 1 / self.policy.rate_limit


class Transport:
    def __init__(
        self,
        provider: ProviderConfig,
        redactor: Redactor,
        http_transport=None,
        shared_gate: RequestGate | None = None,
    ):
        self.provider = provider
        self.policy = provider.policy
        self.redactor = redactor
        self.gate = RequestGate(self.policy)
        self.shared_gate = shared_gate
        self.clients = {}
        self.http_transport = http_transport

    @property
    def budget_exhausted(self):
        return self.gate.sent >= self.policy.max_requests or (
            self.shared_gate is not None
            and self.shared_gate.sent >= self.shared_gate.policy.max_requests
        )

    @asynccontextmanager
    async def request_slot(self):
        async with self.gate.semaphore:
            if self.shared_gate is None:
                await self.gate.admit()
                yield
            else:
                async with self.shared_gate.semaphore:
                    await self.shared_gate.admit(self.gate)
                    yield

    async def __aenter__(self):
        try:
            for endpoint in self.provider.endpoints:
                verify = ssl.create_default_context(cafile=endpoint.ca_bundle)
                self.clients[endpoint.id] = httpx.AsyncClient(
                    proxy=endpoint.proxy,
                    headers=headers(endpoint),
                    timeout=self.policy.timeout,
                    verify=verify,
                    trust_env=False,
                    follow_redirects=False,
                    limits=httpx.Limits(max_connections=self.policy.concurrency),
                    transport=self.http_transport,
                )
        except Exception:
            await self.__aexit__(None, None, None)
            raise
        return self

    async def __aexit__(self, *args):
        for client in self.clients.values():
            await client.aclose()

    async def _once(
        self, endpoint, method, path, body, params, *, stream=False, json_decoder=json.loads
    ) -> HttpOutcome:
        started = time.perf_counter()
        status = None
        retry_after = None
        request_headers = (
            {"Accept": "text/event-stream", "Accept-Encoding": "identity"} if stream else None
        )
        try:
            async with asyncio.timeout(self.policy.timeout):
                async with self.clients[endpoint.id].stream(
                    method,
                    f"{endpoint.base_url}/{path}",
                    json=body,
                    params=params,
                    headers=request_headers,
                ) as response:
                    status = response.status_code
                    retry_after = response.headers.get("retry-after")
                    if stream and status == 200:
                        mime = response.headers.get("content-type", "").split(";", 1)[0]
                        encoding = response.headers.get("content-encoding", "identity")
                        if mime.strip().lower() != "text/event-stream":
                            raise InvalidResponse(
                                "Expected a text/event-stream response", "invalid_stream"
                            )
                        if encoding.strip().lower() != "identity":
                            raise InvalidResponse(
                                "Expected an uncompressed event stream", "invalid_stream"
                            )
                        stop = await validate_stream(
                            endpoint.protocol,
                            response.aiter_bytes(),
                            self.policy.max_response_bytes,
                        )
                        outcome = HttpOutcome(status, stop_reason=stop)
                    else:
                        decoder = json_decoder if status == 200 else json.loads
                        outcome = await self._read_json(response, retry_after, decoder)
        except InvalidResponse as exc:
            outcome = HttpOutcome(
                status,
                category=ErrorCategory.BAD_RESPONSE,
                message=str(exc),
                diagnostic_code=exc.code,
                suggestion=exc.suggestion,
                stop_reason=exc.stop_reason,
            )
        except (TimeoutError, httpx.TimeoutException):
            outcome = HttpOutcome(
                status, category=ErrorCategory.TIMEOUT, message="Request timed out"
            )
        except httpx.HTTPError as exc:
            outcome = HttpOutcome(
                status,
                category=ErrorCategory.NETWORK_ERROR,
                message=f"Network request failed ({type(exc).__name__})",
            )
            if stream and status == 200:
                outcome.diagnostic_code = "stream_interrupted"
                outcome.suggestion = SUGGESTIONS["stream_interrupted"]
        outcome.latency_ms = (time.perf_counter() - started) * 1000
        return outcome

    async def _read_json(self, response, retry_after, decode) -> HttpOutcome:
        status = response.status_code
        content = bytearray()
        async for chunk in response.aiter_bytes(chunk_size=65536):
            content.extend(chunk)
            if len(content) > self.policy.max_response_bytes:
                return HttpOutcome(
                    status,
                    category=ErrorCategory.BAD_RESPONSE if status == 200 else None,
                    message="Response exceeded size limit",
                    retry_after=retry_after,
                )
        try:
            data = decode(content)
        except (ValueError, UnicodeError, RecursionError):
            data = None
        outcome = HttpOutcome(status, data=data, retry_after=retry_after)
        if status == 200 and data is None:
            outcome.category = ErrorCategory.BAD_RESPONSE
            outcome.message = "HTTP 200 did not contain valid JSON"
        return outcome

    async def exchange(
        self,
        endpoint: EndpointConfig,
        method: str,
        path: str,
        *,
        body=None,
        params=None,
        stage="verify",
        on_attempt=None,
        stream=False,
        json_decoder=json.loads,
    ) -> HttpOutcome:
        started = time.perf_counter()
        last = None
        for attempt in range(self.policy.retries + 1):
            try:
                async with self.request_slot():
                    if on_attempt:
                        on_attempt(attempt + 1)
                    outcome = await self._once(
                        endpoint,
                        method,
                        path,
                        body,
                        params,
                        stream=stream,
                        json_decoder=json_decoder,
                    )
                    if outcome.category is None and outcome.http_status != 200:
                        outcome.category = classify_error(outcome.http_status, outcome.data, stage)
                        outcome.message = outcome.message or error_message(outcome.data)
                    outcome.attempts = attempt + 1
                    outcome.message = self.redactor.text(outcome.message, 400)
                    delay = max(
                        retry_after_seconds(outcome.retry_after),
                        self.policy.backoff_base
                        * (2 ** min(attempt, 16))
                        * random.uniform(0.8, 1.2),
                    )
                    if outcome.http_status == 429:
                        (self.shared_gate or self.gate).cooldown(delay)
            except BudgetExhausted:
                if last is None:
                    raise
                outcome = last
                outcome.retry_budget_exhausted = True
                break
            last = outcome
            retryable = outcome.category == ErrorCategory.TIMEOUT or (
                outcome.http_status == 429
                or (outcome.http_status is not None and 500 <= outcome.http_status <= 599)
            )
            outcome.retry_budget_exhausted = (
                retryable
                and outcome.http_status not in (401, 402, 403, 404)
                and attempt < self.policy.retries
                and self.budget_exhausted
            )
            if (
                not retryable
                or outcome.http_status in (401, 402, 403, 404)
                or attempt == self.policy.retries
                or self.budget_exhausted
            ):
                break
            await asyncio.sleep(delay)
        outcome.elapsed_ms = (time.perf_counter() - started) * 1000
        return outcome
