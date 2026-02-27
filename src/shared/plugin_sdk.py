"""Plugin SDK — what plugin authors import."""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import subprocess
from contextvars import ContextVar
from dataclasses import dataclass, field

from threading import Lock
from time import monotonic
from typing import Any

log = logging.getLogger(__name__)


@dataclass
class PluginContext:
    name: str
    secrets: dict[str, str] = field(default_factory=dict)


_plugin_ctx: ContextVar[PluginContext] = ContextVar("_plugin_ctx")


def set_plugin_context(ctx: PluginContext) -> Any:
    return _plugin_ctx.set(ctx)


def reset_plugin_context(token: Any) -> None:
    _plugin_ctx.reset(token)


def get_plugin_context() -> PluginContext:
    return _plugin_ctx.get()


# ---------------------------------------------------------------------------
# 1Password backend
# ---------------------------------------------------------------------------

# Vault name to read from — override with OP_VAULT env var.
_OP_VAULT = os.environ.get("OP_VAULT", "AI-V2")

# Cache: key → (value, expiry_monotonic)
_op_cache: dict[str, tuple[str, float]] = {}
_op_cache_lock = Lock()
_OP_CACHE_TTL = 300  # 5 minutes

# None = not checked yet, True/False = result of first check
_op_available: bool | None = None

# Whether 1Password lookups are enabled. Must be explicitly enabled via
# ``enable_op_backend(True)`` — disabled by default to avoid interactive
# prompts during plugin discovery or automated workflows.
_op_enabled: bool = False


def enable_op_backend(enabled: bool = True) -> None:
    """Explicitly enable or disable 1Password secret lookups."""
    global _op_enabled
    _op_enabled = enabled


def _ensure_op_auth() -> bool:
    """Ensure the 1Password CLI is available and has credentials.

    Returns False immediately if the 1Password backend has not been explicitly
    enabled via ``enable_op_backend(True)``.

    In containers the entrypoint.sh handles signin/signout and exports secrets
    as env vars before the process starts — op CLI is not needed at runtime.
    Locally, the user should have an active ``op signin`` session.
    Returns True if op CLI is available. Result is cached for the process lifetime.
    """
    global _op_available

    if not _op_enabled:
        return False

    if _op_available is not None:
        return _op_available

    if not shutil.which("op"):
        _op_available = False
        return False

    _op_available = True
    return True


def _op_read(key: str) -> str | None:
    """Fetch a secret from 1Password. Results are cached with a TTL."""
    if not _ensure_op_auth():
        return None

    now = monotonic()
    with _op_cache_lock:
        cached = _op_cache.get(key)
        if cached is not None:
            value, expiry = cached
            if now < expiry:
                return value
            del _op_cache[key]

    ref = f"op://{_OP_VAULT}/{key}/password"
    try:
        result = subprocess.run(
            ["op", "read", ref, "--no-newline"],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None

    if result.returncode != 0:
        log.debug("op read %s failed: %s", ref, result.stderr.strip())
        return None

    value = result.stdout
    with _op_cache_lock:
        _op_cache[key] = (value, monotonic() + _OP_CACHE_TTL)
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def secret(key: str, default: str | None = None) -> str:
    """Get a secret. Resolution order: plugin context → 1Password → os.environ.

    - **PluginContext**: Set by PluginManager, populated from .env files (if any).
    - **1Password**: On-demand via ``op read``, cached in-memory with 5min TTL.
      Requires ``op`` CLI — locally via interactive session, in containers
      via entrypoint.sh (signs in, loads secrets as env vars, signs out).
    - **os.environ**: Final fallback for standalone CLI, Docker env, k8s, etc.
    """
    # 1. Check plugin context if available (server mode)
    try:
        ctx = _plugin_ctx.get()
        val = ctx.secrets.get(key)
        if val is not None:
            return val
    except LookupError:
        pass

    # 2. 1Password (on-demand, cached in-memory only)
    val = _op_read(key)
    if val is not None:
        return val

    # 3. Fall back to os.environ (standalone CLI, Docker, k8s)
    val = os.environ.get(key)
    if val is not None:
        return val

    if default is not None:
        return default

    ctx_name = ""
    with contextlib.suppress(LookupError):
        ctx_name = f" for plugin '{_plugin_ctx.get().name}'"
    raise KeyError(f"Missing secret '{key}'{ctx_name}")
