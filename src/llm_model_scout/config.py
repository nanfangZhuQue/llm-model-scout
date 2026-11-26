from __future__ import annotations

import math
import os
import re
import tomllib
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from .domain import CAPABILITIES, PROTOCOLS, EndpointConfig, ProbeConfig, ProviderConfig, ScanPolicy


class ConfigError(ValueError):
    pass


def normalize_url(value: str) -> str:
    try:
        url = urlsplit(value)
        _ = url.port
    except ValueError:
        raise ConfigError("Invalid base URL") from None
    if (
        url.scheme not in ("https", "http")
        or not url.hostname
        or url.username is not None
        or url.password is not None
        or url.query
        or url.fragment
        or any(c.isspace() or ord(c) < 32 for c in value)
    ):
        raise ConfigError("Base URL must be HTTP(S), without credentials, query or fragment")
    path = url.path.rstrip("/") or "/v1"
    return urlunsplit((url.scheme, url.netloc, path, "", ""))


def validate_proxy(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        url = urlsplit(value)
        if (
            url.scheme not in ("socks5", "socks5h", "http", "https")
            or not url.hostname
            or not url.port
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError
    except (ValueError, TypeError):
        raise ConfigError("Proxy must be http(s):// or socks5(h)://[user:pass@]host:port") from None
    return value


def model_ids(values: object) -> tuple[str, ...]:
    if not isinstance(values, (list, tuple)) or any(not isinstance(v, str) for v in values):
        raise ConfigError("models must be a list of strings")
    return tuple(dict.fromkeys(v.strip() for v in values if v.strip()))


def read_models(path: Path) -> tuple[str, ...]:
    try:
        return model_ids(path.read_text(encoding="utf-8-sig").splitlines())
    except (OSError, UnicodeError):
        raise ConfigError("Cannot read models file as UTF-8") from None


def _number(name: str, value: object, integer: bool = False, zero: bool = False):
    try:
        if isinstance(value, bool):
            raise ValueError
        n = float(value)
        if not math.isfinite(n) or (n < 0 if zero else n <= 0):
            raise ValueError
        if integer and not n.is_integer():
            raise ValueError
        return int(n) if integer else n
    except (ValueError, TypeError):
        raise ConfigError(f"Invalid numeric setting: {name}") from None


def _token_budget(value: object) -> int:
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value.strip()):
        try:
            value = int(value)
        except ValueError:
            raise ConfigError("max_tokens must be a positive integer") from None
    if type(value) is not int or value <= 0:
        raise ConfigError("max_tokens must be a positive integer")
    return value


def load_provider(options: dict, environ: dict | None = None) -> ProviderConfig:
    env = os.environ if environ is None else environ
    config_path = options.get("config")
    base_dir = Path.cwd()
    provider: dict = {}
    if config_path:
        path = Path(config_path)
        base_dir = path.resolve().parent
        try:
            root = tomllib.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, tomllib.TOMLDecodeError):
            raise ConfigError("Cannot read configuration as TOML") from None
        providers = root.get("providers", [])
        if not isinstance(providers, list) or any(not isinstance(p, dict) for p in providers):
            raise ConfigError("providers must be an array of tables")
        selection = options.get("provider") or env.get("SCOUT_PROVIDER")
        matches = [p for p in providers if selection is None or p.get("name") == selection]
        if len(matches) != 1:
            raise ConfigError("Select exactly one provider using --provider")
        provider = matches[0]
    elif options.get("provider"):
        raise ConfigError("--provider requires --config")

    def setting(name, scope, default=None):
        if options.get(name) is not None:
            return options[name]
        key = "SCOUT_" + name.upper()
        return env.get(key, scope.get(name, default))

    defaults = ScanPolicy()
    policy_values = {}
    for name in ("concurrency", "retries", "max_requests", "max_models", "max_pages"):
        policy_values[name] = _number(
            name,
            setting(name, provider, getattr(defaults, name)),
            integer=True,
            zero=name == "retries",
        )
    for name in ("timeout", "rate_limit", "scan_timeout"):
        value = setting(name, provider, getattr(defaults, name))
        policy_values[name] = None if value is None else _number(name, value)
    policy = ScanPolicy(**policy_values)
    rows = provider.get("endpoints", [{}] if not config_path else [])
    if not isinstance(rows, list) or not rows or any(not isinstance(r, dict) for r in rows):
        raise ConfigError("Provider must contain endpoint tables")
    ids = [r.get("id", "default") for r in rows]
    if len(set(ids)) != len(ids):
        raise ConfigError("Endpoint ids must be unique")
    selected = options.get("endpoint") or env.get("SCOUT_ENDPOINT")
    if selected != "all":
        rows = [r for r in rows if selected is None or r.get("id", "default") == selected]
        if len(rows) != 1:
            raise ConfigError("Select one --endpoint, or explicitly use --endpoint all")
    # A global address or credential override on multiple endpoints can route secrets incorrectly.
    if len(rows) > 1 and any(
        options.get(k) is not None or env.get("SCOUT_" + k.upper()) is not None
        for k in ("base_url", "protocol", "api_key", "api_key_env")
    ):
        raise ConfigError("Address/protocol/credential overrides require a single endpoint")
    manual = options.get("model")
    manual_file = options.get("models_file") or env.get("SCOUT_MODELS_FILE")
    if manual and manual_file:
        raise ConfigError("Use --model or --models-file, not both")
    endpoints = []
    for row in rows:
        endpoint_id = row.get("id", "default")
        if not isinstance(endpoint_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", endpoint_id):
            raise ConfigError("Endpoint id must use letters, digits, dots, underscores or hyphens")
        protocol = setting("protocol", row)
        if protocol not in PROTOCOLS:
            raise ConfigError(f"--protocol must be one of: {', '.join(PROTOCOLS)}")
        address = setting("base_url", row)
        if not isinstance(address, str):
            raise ConfigError("A base_url is required")
        key_ref = setting("api_key_env", row)
        key = setting("api_key", row)
        if key_ref and key:
            raise ConfigError("Use either api_key_env or api_key, not both")
        if key_ref:
            if not isinstance(key_ref, str):
                raise ConfigError("api_key_env must name an environment variable")
            key = env.get(key_ref)
        if not isinstance(key, str) or not key or any(ord(c) < 32 for c in key):
            raise ConfigError("A non-empty API key environment variable is required")
        proxy = setting("proxy", row, provider.get("proxy"))
        proxy_ref = setting("proxy_env", row, provider.get("proxy_env"))
        if proxy_ref:
            if proxy:
                raise ConfigError("Use either proxy or proxy_env, not both")
            proxy = env.get(proxy_ref)
            if not proxy:
                raise ConfigError("Proxy environment variable is empty")
        auth = row.get("auth", "default")
        if auth not in ("default", "bearer", "x-api-key"):
            raise ConfigError("auth must be default, bearer or x-api-key")
        version = row.get("api_version", "2023-06-01")
        if not isinstance(version, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", version):
            raise ConfigError("api_version must be a YYYY-MM-DD value")
        models = None
        if manual:
            models = model_ids(manual)
        elif manual_file:
            models = read_models(Path(manual_file))
        elif "models" in row:
            models = model_ids(row["models"])
        elif "models_file" in row:
            models = read_models(base_dir / row["models_file"])
        capability = setting("capability", row, "text")
        if capability not in CAPABILITIES:
            raise ConfigError(f"--capability must be one of: {', '.join(CAPABILITIES)}")
        profile = setting("profile", row)
        uses_fixed_schema = capability in ("structured", "tool")
        default_profile = (
            "chat-basic" if uses_fixed_schema or protocol == "openai_responses" else "baseline"
        )
        try:
            probe = ProbeConfig.named(default_profile if profile is None else profile)
        except ValueError:
            raise ConfigError("profile must be baseline or chat-basic") from None
        default_tokens = 128 if uses_fixed_schema and profile is None else probe.max_tokens
        probe = replace(
            probe,
            max_tokens=_token_budget(setting("max_tokens", row, default_tokens)),
            capability=capability,
        )
        ca_bundle = setting("ca_bundle", row)
        if ca_bundle:
            ca_bundle = str(base_dir / ca_bundle)
            if not Path(ca_bundle).is_file():
                raise ConfigError("CA bundle does not exist")
        endpoints.append(
            EndpointConfig(
                endpoint_id,
                protocol,
                normalize_url(address),
                key,
                validate_proxy(proxy),
                auth,
                version,
                models,
                probe,
                ca_bundle,
            )
        )
    name = provider.get("name", "default")
    if not isinstance(name, str) or not name.strip():
        raise ConfigError("Provider name must be non-empty")
    return ProviderConfig(name, tuple(endpoints), policy)
