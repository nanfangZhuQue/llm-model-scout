from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Literal, get_args


class ErrorCategory(StrEnum):
    OK = "OK"
    NETWORK_ERROR = "NETWORK_ERROR"
    TIMEOUT = "TIMEOUT"
    UNAUTHORIZED = "UNAUTHORIZED"
    FORBIDDEN = "FORBIDDEN"
    PAYMENT_REQUIRED = "PAYMENT_REQUIRED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    RATE_LIMITED = "RATE_LIMITED"
    SERVER_ERROR = "SERVER_ERROR"
    BAD_RESPONSE = "BAD_RESPONSE"
    UNKNOWN_ERROR = "UNKNOWN_ERROR"


Protocol = Literal["openai_chat", "anthropic_messages", "openai_responses"]
Capability = Literal["text", "stream", "structured", "tool"]
PROTOCOLS = get_args(Protocol)
CAPABILITIES = get_args(Capability)
STRUCTURED_PROMPT = 'Return only a JSON object with status "ok" and count 1.'
TOOL_PROMPT = 'Call scout_tool_probe exactly once with value "ping" and count 1.'
_FIXED_PROMPTS = {"structured": STRUCTURED_PROMPT, "tool": TOOL_PROMPT}
DEFAULT_AUTH = {
    "openai_chat": "bearer",
    "anthropic_messages": "x-api-key",
    "openai_responses": "bearer",
}


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True)
class ProbeConfig:
    profile_id: str = "baseline"
    max_tokens: int = 1
    temperature: float | None = 0
    prompt: str = "ping"
    capability: Capability = "text"

    def __post_init__(self):
        if type(self.max_tokens) is not int or self.max_tokens <= 0:
            raise ValueError("max_tokens must be a positive integer")
        if self.capability not in CAPABILITIES:
            raise ValueError(f"capability must be one of: {', '.join(CAPABILITIES)}")

    def record(self) -> dict:
        return {**asdict(self), "prompt": _FIXED_PROMPTS.get(self.capability, self.prompt)}

    @classmethod
    def named(cls, name: str) -> ProbeConfig:
        if name == "baseline":
            return cls()
        if name == "chat-basic":
            return cls(name, 16, None)
        raise ValueError("profile must be baseline or chat-basic")


@dataclass(frozen=True)
class EndpointConfig:
    id: str
    protocol: Protocol
    base_url: str
    api_key: str = field(repr=False)
    proxy: str | None = field(default=None, repr=False)
    auth: str = "default"
    api_version: str = "2023-06-01"
    models: tuple[str, ...] | None = None
    probe: ProbeConfig = field(default_factory=ProbeConfig)
    ca_bundle: str | None = None


@dataclass(frozen=True)
class ScanPolicy:
    concurrency: int = 10
    timeout: float = 30
    retries: int = 2
    max_requests: int = 1000
    max_models: int = 1000
    scan_timeout: float | None = None
    rate_limit: float | None = None
    max_response_bytes: int = 2 * 1024 * 1024
    max_pages: int = 100
    backoff_base: float = 0.5


@dataclass(frozen=True)
class ProviderConfig:
    name: str
    endpoints: tuple[EndpointConfig, ...]
    policy: ScanPolicy = field(default_factory=ScanPolicy)


@dataclass
class HttpOutcome:
    http_status: int | None
    data: object = None
    category: ErrorCategory | None = None
    message: str = ""
    attempts: int = 0
    latency_ms: float = 0
    elapsed_ms: float = 0
    retry_after: str | None = None
    retry_budget_exhausted: bool = False
    diagnostic_code: str | None = None
    suggestion: str | None = None
    stop_reason: str | None = None


@dataclass
class DiscoveryResult:
    endpoint_id: str
    protocol: str
    source: str
    status: str = "completed"
    models: list[str] = field(default_factory=list)
    error_category: ErrorCategory | None = None
    error_message: str = ""
    http_status: int | None = None
    pages: int = 0


@dataclass
class ModelResult:
    endpoint_id: str
    protocol: str
    model: str
    probe_id: str
    status: str = "skipped"
    http_status: int | None = None
    error_category: ErrorCategory | None = None
    error_message: str = ""
    reason: str = "not_started"
    attempts: int = 0
    latency_ms: float | None = None
    elapsed_ms: float | None = None
    timestamp: str = field(default_factory=utc_now)
    diagnostic_code: str | None = None
    suggestion: str | None = None
    stop_reason: str | None = None
    capability: Capability = "text"

    def record(self) -> dict:
        row = asdict(self)
        if self.capability == "text":
            del row["capability"]
        return row


@dataclass
class ScanReport:
    provider: str
    endpoints: list[dict]
    policy: dict
    status: str = "completed"
    started_at: str = field(default_factory=utc_now)
    finished_at: str = ""
    requests_sent: int | None = 0
    discovery: list[DiscoveryResult] = field(default_factory=list)
    results: list[ModelResult] = field(default_factory=list)
    reason: str = ""
