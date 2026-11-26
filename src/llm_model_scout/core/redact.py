from __future__ import annotations

import base64
import re
from urllib.parse import quote, unquote, urlsplit

from ..domain import ProviderConfig


class Redactor:
    def __init__(self, secrets=()):
        variants = set()
        for secret in secrets:
            if secret:
                variants.update((secret, quote(secret, safe=""), unquote(secret)))
        self.secrets = sorted((s for s in variants if s), key=len, reverse=True)

    @classmethod
    def for_provider(cls, provider: ProviderConfig, *, include_short_proxy_secrets=False):
        secrets = []
        for endpoint in provider.endpoints:
            secrets.append(endpoint.api_key)
            if endpoint.proxy:
                parts = urlsplit(endpoint.proxy)
                secrets.append(endpoint.proxy)
                # Short proxy credentials must not corrupt enum names or timestamps.
                secrets.extend(
                    value
                    for value in (parts.username, parts.password)
                    if value and (include_short_proxy_secrets or len(value) >= 4)
                )
                if parts.username is not None:
                    credentials = f"{unquote(parts.username)}:{unquote(parts.password or '')}"
                    secrets.append(credentials)
                    secrets.append(base64.b64encode(credentials.encode()).decode())
        return cls(secrets)

    def text(self, value: str, limit: int | None = None) -> str:
        text = str(value)
        for secret in self.secrets:
            text = text.replace(secret, "***REDACTED***")
        text = re.sub(r"(?i)Bearer\s+[^\s\"',;]+", "Bearer ***REDACTED***", text)
        text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", text)
        text = "".join(c for c in text if ord(c) >= 32 and ord(c) != 127)
        return text if limit is None else text[:limit]

    def clean(self, value):
        if isinstance(value, str):
            return self.text(value)
        if isinstance(value, list):
            return [self.clean(v) for v in value]
        if isinstance(value, dict):
            return {k: self.clean(v) for k, v in value.items()}
        return value
