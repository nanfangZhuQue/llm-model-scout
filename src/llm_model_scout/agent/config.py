import os
import re
import secrets
import tomllib
from pathlib import Path

from ..config import ConfigError, load_provider


def load_registry(path: Path, environ=None):
    env = os.environ if environ is None else environ
    try:
        root = tomllib.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError):
        raise ConfigError("Cannot read Agent provider configuration") from None
    providers = root.get("providers")
    if not isinstance(providers, list) or not providers:
        raise ConfigError("Agent requires at least one configured provider")
    registry = {}
    for row in providers:
        if not isinstance(row, dict):
            raise ConfigError("Invalid provider table")
        name = row.get("name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", name):
            raise ConfigError("Agent provider name must be a safe identifier")
        if name in registry:
            raise ConfigError("Duplicate provider name")
        endpoints = row.get("endpoints", [])
        if not isinstance(endpoints, list) or any(not isinstance(e, dict) for e in endpoints):
            raise ConfigError("Invalid endpoint tables")
        refs = []
        for scope in (row, *endpoints):
            for field in ("api_key_env", "proxy_env"):
                ref = scope.get(field)
                if ref is not None:
                    if not isinstance(ref, str):
                        raise ConfigError("Credential references must name environment variables")
                    reserved = {
                        "SCOUT_" + key.upper()
                        for key in (
                            "provider",
                            "endpoint",
                            "protocol",
                            "base_url",
                            "api_key",
                            "api_key_env",
                            "proxy",
                            "proxy_env",
                            "profile",
                            "capability",
                            "ca_bundle",
                            "concurrency",
                            "timeout",
                            "retries",
                            "max_requests",
                            "max_models",
                            "max_pages",
                            "rate_limit",
                            "scan_timeout",
                            "max_tokens",
                            "models_file",
                        )
                    }
                    if ref in reserved:
                        raise ConfigError(
                            "Credential reference collides with a reserved SCOUT setting"
                        )
                    refs.append(ref)
        # Do not let unrelated process SCOUT_* values replace administrator-approved routes.
        credential_env = {name: env[name] for name in refs if name in env}
        registry[name] = load_provider(
            {"config": path, "provider": name, "endpoint": "all"}, credential_env
        )
    return registry


def prepare_token(
    token_env: str | None, token_file: Path | None, *, token: str | None = None, environ=None
):
    env = os.environ if environ is None else environ
    if token is not None:
        if token_env or token_file is not None:
            raise ConfigError("Use token, token-env, or token-file exclusively")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ConfigError("Agent token must be 32-256 URL-safe characters")
        return token, None
    if token_env:
        if token_file is not None:
            raise ConfigError("Use token-env or token-file, not both")
        token = env.get(token_env, "")
        if not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{32,256}", token):
            raise ConfigError("Agent token must be 32-256 URL-safe characters")
        return token, None
    path = Path(token_file or ".scout/agent.token")
    token = secrets.token_urlsafe(32)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise ConfigError(
            "Cannot exclusively create token file; use a new path or token-env"
        ) from None
    try:
        with os.fdopen(fd, "w", encoding="ascii", newline="") as stream:
            stream.write(token + "\n")
    except OSError:
        path.unlink(missing_ok=True)
        raise ConfigError("Cannot write Agent token file") from None
    return token, path


def remove_owned_token(path: Path | None, token: str):
    if path is None:
        return
    try:
        if not path.is_symlink() and path.read_text(encoding="ascii") == token + "\n":
            path.unlink()
    except (OSError, UnicodeError):
        pass
