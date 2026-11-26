"""User-managed provider definitions and encrypted, revisioned local storage."""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .config import ConfigError, normalize_url, validate_proxy
from .core.redact import Redactor
from .domain import EndpointConfig, ProbeConfig, ProviderConfig, ScanPolicy

MANAGED_ENDPOINT_IDS = {
    "openai_chat": "openai",
    "anthropic_messages": "anthropic",
    "openai_responses": "responses",
}


class StoreError(ValueError):
    pass


class StoreConflict(StoreError):
    pass


def default_store_path():
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    return base / "ModelScout/providers.sqlite3"


class Protector:
    def __init__(self, path: Path):
        self.identity = hashlib.sha256(str(path.resolve()).encode()).hexdigest()

    def _windows(self, data: bytes, decrypt: bool):
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        buffer = ctypes.create_string_buffer(data)
        source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [
            ctypes.POINTER(Blob),
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(Blob),
        ]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        if not function(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output)):
            raise StoreError("Cannot protect/unlock local data for this Windows account")
        try:
            return ctypes.string_at(output.data, output.size)
        finally:
            kernel.LocalFree(output.data)

    def _fernet(self, create=False):
        try:
            import keyring
            from cryptography.fernet import Fernet

            backend = keyring.get_keyring()
            candidates = getattr(backend, "backends", [backend])
            secure = next(
                (
                    b
                    for b in candidates
                    if b.__class__.__module__.startswith(
                        (
                            "keyring.backends.macOS",
                            "keyring.backends.SecretService",
                            "keyring.backends.kwallet",
                        )
                    )
                ),
                None,
            )
            if secure is None:
                raise StoreError(
                    "An OS-backed keyring is required; plaintext fallback is forbidden"
                )
            service = "llm-model-scout-" + self.identity
            key = secure.get_password(service, "provider-store")
            if key is None and create:
                key = Fernet.generate_key().decode("ascii")
                secure.set_password(service, "provider-store", key)
            if key is None:
                raise StoreError("Local encryption key is unavailable")
            return Fernet(key.encode("ascii"))
        except StoreError:
            raise
        except Exception:
            raise StoreError(
                "Secure storage unavailable; install secure-store extras and an OS keyring"
            ) from None

    def encrypt(self, data: bytes):
        if os.name == "nt":
            return b"DPAPI1\0" + self._windows(data, False)
        return b"FERNET1\0" + self._fernet(create=True).encrypt(data)

    def decrypt(self, data: bytes):
        try:
            if data.startswith(b"DPAPI1\0") and os.name == "nt":
                return self._windows(data[7:], True)
            if data.startswith(b"FERNET1\0"):
                return self._fernet().decrypt(data[8:])
        except StoreError:
            raise
        except Exception:
            raise StoreError("Cannot unlock local provider data") from None
        raise StoreError("Encrypted data belongs to an unsupported platform or format")


def definition_to_provider(definition: dict) -> ProviderConfig:
    name = definition.get("name")
    rows = definition.get("endpoints")
    if not isinstance(name, str) or not 1 <= len(name.strip()) <= 80:
        raise ConfigError("Provider name must contain 1-80 characters")
    if not isinstance(rows, list) or not 1 <= len(rows) <= len(MANAGED_ENDPOINT_IDS):
        raise ConfigError(f"Provide 1 to {len(MANAGED_ENDPOINT_IDS)} protocol endpoints")
    endpoints = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            raise ConfigError("Invalid endpoint definition")
        protocol = row.get("protocol")
        if (
            not isinstance(protocol, str)
            or protocol not in MANAGED_ENDPOINT_IDS
            or protocol in seen
        ):
            raise ConfigError("Each supported protocol may appear only once")
        seen.add(protocol)
        key = row.get("api_key")
        if (
            not isinstance(key, str)
            or not key
            or len(key) > 4096
            or not key.isascii()
            or any(c.isspace() or ord(c) < 32 for c in key)
        ):
            raise ConfigError("A non-empty ASCII API key without whitespace is required")
        address = row.get("base_url")
        if not isinstance(address, str) or len(address) > 2048:
            raise ConfigError("Invalid base URL")
        proxy = row.get("proxy") or None
        if proxy is not None and (not isinstance(proxy, str) or len(proxy) > 2048):
            raise ConfigError("Invalid proxy URL")
        endpoints.append(
            EndpointConfig(
                MANAGED_ENDPOINT_IDS[protocol],
                protocol,
                normalize_url(address),
                key,
                proxy=validate_proxy(proxy),
                probe=ProbeConfig("chat-basic", 256, None),
            )
        )
    return ProviderConfig(
        name.strip(), tuple(endpoints), ScanPolicy(max_models=10000, max_requests=10000)
    )


def canonical_definition(definition):
    provider = definition_to_provider(definition)
    return {
        "name": provider.name,
        "endpoints": [
            {"protocol": e.protocol, "base_url": e.base_url, "api_key": e.api_key, "proxy": e.proxy}
            for e in provider.endpoints
        ],
    }


def export_definition(definition: dict, include_secrets=False):
    canonical = canonical_definition(definition)
    if include_secrets:
        return {"schema_version": 1, "contains_secrets": True, "provider": canonical}
    for endpoint in canonical["endpoints"]:
        endpoint["api_key"] = "***REDACTED***"
        if endpoint["proxy"]:
            parsed = urlsplit(endpoint["proxy"])
            host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
            endpoint["proxy"] = urlunsplit((parsed.scheme, f"{host}:{parsed.port}", "", "", ""))
    return {
        "schema_version": 1,
        "contains_secrets": False,
        "provider": Redactor.for_provider(definition_to_provider(definition)).clean(canonical),
    }


class ProviderStore:
    def __init__(self, path: Path | None = None, protector=None):
        self.path = Path(path or default_store_path())
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.protector = protector or Protector(self.path)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS providers "
                "(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, payload BLOB NOT NULL)"
            )

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def records(self):
        try:
            with self._connect() as db:
                rows = db.execute(
                    "SELECT id, revision, payload FROM providers ORDER BY rowid"
                ).fetchall()
            return {
                key: {
                    "revision": revision,
                    "definition": json.loads(self.protector.decrypt(payload)),
                }
                for key, revision, payload in rows
            }
        except (sqlite3.Error, ValueError, TypeError):
            raise StoreError("Cannot read/unlock local provider store") from None

    def put(self, definition, provider_id=None, revision=None):
        canonical = canonical_definition(definition)
        provider_id = provider_id or "p_" + uuid.uuid4().hex
        try:
            with self._connect() as db:
                db.execute("BEGIN IMMEDIATE")
                row = db.execute(
                    "SELECT revision FROM providers WHERE id=?", (provider_id,)
                ).fetchone()
                if row is not None and revision != row[0]:
                    raise StoreConflict("Provider changed; refresh before saving")
                if row is None and revision is not None:
                    raise StoreConflict("Provider no longer exists")
                if (
                    row is None
                    and db.execute("SELECT count(*) FROM providers").fetchone()[0] >= 100
                ):
                    raise StoreError("At most 100 providers may be saved")
                current = (row[0] + 1) if row else 1
                # Only ciphertext reaches SQLite, including its journals.
                payload = self.protector.encrypt(json.dumps(canonical, ensure_ascii=False).encode())
                db.execute(
                    "INSERT OR REPLACE INTO providers VALUES (?, ?, ?)",
                    (provider_id, current, payload),
                )
            return provider_id
        except sqlite3.Error:
            raise StoreError("Could not save local provider data") from None

    def delete(self, provider_id, revision):
        try:
            with self._connect() as db:
                cursor = db.execute(
                    "DELETE FROM providers WHERE id=? AND revision=?", (provider_id, revision)
                )
                if cursor.rowcount != 1:
                    raise StoreConflict("Provider changed or was removed; refresh first")
        except sqlite3.Error:
            raise StoreError("Could not delete local provider data") from None
