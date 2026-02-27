"""Smoke test all plugin integrations with real API keys.

Loads all plugins once, then calls one safe read-only method per plugin
to verify API keys and connectivity.
"""

from __future__ import annotations

import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
from pathlib import Path

# Suppress noisy logs during discovery
os.environ.setdefault("AI_V2_LOG_LEVEL", "critical")

from shared.plugin_manager import PluginManager

# Each entry: (plugin_name, tool_name, args_dict)
# Pick safe, read-only, fast methods for each plugin.
TESTS: list[tuple[str, str, dict]] = [
    # ── Public / Free APIs ──
    ("coindesk", "news", {}),
    ("congress", "list_bills", {}),
    ("defillama", "list_protocols", {}),
    ("fedreg", "get_agencies", {}),
    ("googlenews", "headlines", {}),
    ("kalshi", "list_series", {}),
    ("opentable", "list_metros", {}),
    ("polymarket", "list_events", {}),
    ("theblock", "news", {}),
    # ── Crypto Data ──
    ("arkham", "health", {}),
    ("coingecko", "get_trending", {}),
    ("coinmetrics", "list_assets", {}),
    ("debank", "get_chain_list", {}),
    ("dune", "get_query", {"query_id": 1}),
    ("messari", "list_assets", {}),
    ("nansen", "search_entity", {"query": "vitalik"}),
    ("tardis", "list_exchanges", {}),
    # ── Enterprise / Finance ──
    ("affinity", "whoami", {}),
    ("alchemy", "get_block_number", {}),
    ("allium", "search_schemas", {"query": "transfers"}),
    ("alphasense", "get_user", {}),
    ("anchorage", "list_vaults", {}),
    ("ashby", "api_key_info", {}),
    ("attio", "whoami", {}),
    ("bitgo", "list_enterprises", {}),
    ("bloomberg", "get_datasets", {}),
    ("coinbase", "list_portfolios", {}),
    ("crunchbase", "search_organizations", {"query": "paradigm"}),
    ("falconx", "list_pairs", {}),
    ("harmonic", "get_saved_searches", {}),
    ("ironclad", "schemas", {}),
    ("sensortower", "search_apps", {"query": "coinbase"}),
    ("similarweb", "get_credits", {}),
    ("standard-metrics", "list_companies", {}),
    # ── Internal / Comms ──
    ("granola", "list_workspaces", {}),
    ("gsuite", "calendar_list", {}),
    ("linear", "me", {}),
    ("notion", "me", {}),
    ("pylon", "get_me", {}),
    ("slack", "list_usergroups", {}),
    ("sigma", "list_workbooks", {}),
    # ── Analytics / Research ──
    ("confmonitor", "get_sheet_data", {}),
    ("legistorm", "get_members", {}),
    ("listennotes", "search", {"query": "crypto"}),
    ("newsapi", "sources", {}),
    ("openfec", "search_candidates", {"query": "smith"}),
    ("posthog", "events", {}),
    ("youtube", "search", {"query": "ethereum"}),
    # ── Internal Tools ──
    ("archiver", "status", {}),
    ("paradigmdb", "db_tables", {}),
    ("ptwittercli", "get_usage", {}),
    ("reth", "get_execution_timings", {}),
    ("social-monitor", "stats", {}),
    ("termsheet", "list_deals", {}),
    ("unit410", "list_wallets", {}),
    # ── Media / Gen AI ──
    ("nano-banana", "list_models", {}),
    ("transcriber", "list_models", {}),
    ("veo3", "list_models", {}),
    # ── Expected missing keys ──
    ("docusign", "envelopes", {}),
    ("figma", "search", {"query": "test"}),
    ("telegram", "list_channels", {}),
]

# Plugins that are expected to fail due to missing API keys
EXPECTED_MISSING = {"affinity", "alchemy", "allium", "attio", "docusign", "figma", "telegram"}

AUTH_KEYWORDS = {
    "api_key", "api key", "token", "secret", "auth", "credential",
    "not set", "not found", "missing", "unauthorized", "forbidden",
    "401", "403", "apikey",
}


def is_auth_error(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in AUTH_KEYWORDS)


def _call_sync(manager: PluginManager, plugin_name: str, tool_name: str, args: dict) -> str:
    """Call a plugin tool synchronously (for use inside a thread)."""
    import asyncio as _aio

    return _aio.run(manager.call_tool(plugin_name, tool_name, args))


def test_plugin(
    executor: ThreadPoolExecutor,
    manager: PluginManager,
    plugin_name: str,
    tool_name: str,
    args: dict,
) -> dict:
    """Test a single plugin tool call with a thread-based timeout."""
    start = time.monotonic()

    if plugin_name not in manager.plugins:
        return {
            "plugin": plugin_name,
            "tool": tool_name,
            "status": "not_loaded",
            "ms": 0,
            "detail": "Plugin failed to load (missing key or import error)",
        }

    try:
        future = executor.submit(_call_sync, manager, plugin_name, tool_name, args)
        result = future.result(timeout=15)
        ms = int((time.monotonic() - start) * 1000)
    except FuturesTimeout:
        return {
            "plugin": plugin_name,
            "tool": tool_name,
            "status": "timeout",
            "ms": 15000,
            "detail": "Timed out after 15s",
        }
    except SystemExit as e:
        ms = int((time.monotonic() - start) * 1000)
        return {
            "plugin": plugin_name,
            "tool": tool_name,
            "status": "error",
            "ms": ms,
            "detail": f"sys.exit({e.code})",
        }
    except Exception as e:
        ms = int((time.monotonic() - start) * 1000)
        detail = str(e)[:200]
        return {
            "plugin": plugin_name,
            "tool": tool_name,
            "status": "auth_error" if is_auth_error(detail) else "error",
            "ms": ms,
            "detail": detail,
        }

    # Parse result
    try:
        parsed = json.loads(result) if isinstance(result, str) else result
    except json.JSONDecodeError:
        parsed = result

    if isinstance(parsed, dict) and "error" in parsed:
        detail = str(parsed["error"])[:200]
        status = "auth_error" if is_auth_error(detail) else "error"
        return {
            "plugin": plugin_name,
            "tool": tool_name,
            "status": status,
            "ms": ms,
            "detail": detail,
        }

    return {
        "plugin": plugin_name,
        "tool": tool_name,
        "status": "ok",
        "ms": ms,
        "detail": "",
    }


def main() -> int:
    app_root = Path(__file__).resolve().parent.parent
    plugins_dir = app_root / "plugins"

    print("Loading plugins...", flush=True)
    manager = PluginManager(plugins_dir)
    manager.discover()
    loaded = set(manager.plugins.keys())
    print(f"Loaded {len(loaded)} plugins\n")

    print("═" * 72)
    print("  Plugin Integration Smoke Tests")
    print("═" * 72)

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        for plugin_name, tool_name, args in TESTS:
            r = test_plugin(executor, manager, plugin_name, tool_name, args)
            results.append(r)

            icon = {
                "ok": "✅",
                "auth_error": "⚠️ ",
                "not_loaded": "⚠️ ",
                "error": "❌",
                "timeout": "⏱️ ",
            }.get(r["status"], "❓")

            line = f"  {icon} {r['plugin']:20s} {r['tool']:25s} {r['ms']:5d}ms"
            if r["detail"]:
                line += f"  {r['detail'][:80]}"
            print(line, flush=True)

    # Summary
    ok = [r for r in results if r["status"] == "ok"]
    auth = [r for r in results if r["status"] in ("auth_error", "not_loaded")]
    errs = [r for r in results if r["status"] == "error"]
    timeouts = [r for r in results if r["status"] == "timeout"]

    unexpected = [r for r in errs if r["plugin"] not in EXPECTED_MISSING]

    print()
    print("═" * 72)
    print(f"  ✅ {len(ok)} passed  |  ⚠️  {len(auth)} auth/skipped  |  "
          f"❌ {len(errs)} errors  |  ⏱️  {len(timeouts)} timeouts")
    print("═" * 72)

    if unexpected:
        print(f"\n  Unexpected failures ({len(unexpected)}):")
        for r in unexpected:
            print(f"    • {r['plugin']}.{r['tool']}: {r['detail'][:120]}")

    if timeouts:
        print(f"\n  Timeouts ({len(timeouts)}):")
        for r in timeouts:
            print(f"    • {r['plugin']}.{r['tool']}")

    return len(unexpected) + len(timeouts)


if __name__ == "__main__":
    rc = main()
    sys.exit(min(rc, 125))
