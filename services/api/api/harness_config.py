from __future__ import annotations

import os
import re


_DEFAULT_HARNESS_ALIASES: dict[str, str] = {
    "amp": "amp",
    "claude": "claude-code",
    "claude-code": "claude-code",
    "codex": "codex",
    "pi": "pi-mono",
    "pi-mono": "pi-mono",
}


def default_harness() -> str:
    raw = (os.getenv("CENTAUR_DEFAULT_HARNESS") or "codex").strip().lower()
    return _DEFAULT_HARNESS_ALIASES.get(raw, "codex")


def enabled_harnesses() -> frozenset[str]:
    """Engines whose harness credentials the deployment is allowed to manage.

    Always includes :func:`default_harness`. A deployment that can also spawn
    other harnesses lists them in ``CENTAUR_ENABLED_HARNESSES`` (comma- or
    whitespace-separated, accepting the same aliases as
    ``CENTAUR_DEFAULT_HARNESS``); each named engine joins the set. Names that
    aren't aliases pass through verbatim so a not-yet-aliased engine still
    works; unrecognized engines simply match no ``_HARNESS_SECRETS`` entry.

    This is the allowlist :meth:`api.tool_manager.ToolManager.collect_secrets`
    gates on so the shared API-side iron-proxy and iron-token-broker only
    manage credentials for harnesses the deployment can actually reach.
    Advertising a brokered credential whose 1Password items don't exist
    corrupts iron-token-broker's shared SDK client and breaks rotation for
    *every* credential, so a deployment must not enable a harness it hasn't
    provisioned secrets for.
    """
    enabled = {default_harness()}
    raw = os.getenv("CENTAUR_ENABLED_HARNESSES") or ""
    for token in re.split(r"[,\s]+", raw.strip().lower()):
        if token:
            enabled.add(_DEFAULT_HARNESS_ALIASES.get(token, token))
    return frozenset(enabled)
