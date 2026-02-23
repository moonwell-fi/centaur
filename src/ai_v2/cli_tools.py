"""CLI tool wrappers — shell out to installed uv-tool CLIs."""

from __future__ import annotations

import asyncio
import json
import shutil

# Path to uv-tool-installed binaries
_BIN = "/home/ubuntu/.local/bin"

# Allowed CLIs and their descriptions for discovery
ALLOWED_CLIS: dict[str, str] = {
    "slack": "Search Slack messages, channels, threads, users",
    "reshift": "Paradigm I&R platform: DB queries, Shift notes, emails, calendar, drive",
    "gsuite": "Gmail, Calendar, Drive, Docs",
    "linear": "Linear issues, projects, cycles",
    "parchiver": "Document archiver: data rooms, memos, DocSend, Drive",
    "allium": "On-chain SQL analytics",
    "coingecko": "Crypto prices and market data",
    "coinmetrics": "On-chain metrics and timeseries",
    "defillama": "DeFi TVL, stablecoins, protocol data",
    "dune": "Execute Dune Analytics queries",
    "posthog": "Product analytics and HogQL queries",
    "attio": "CRM data from Attio",
    "ashby": "Recruiting data from Ashby",
    "affinity": "Deal flow and relationship data",
    "anchorage": "Custody data from Anchorage",
    "bitgo": "Custody data from BitGo",
    "coinbase": "Coinbase data",
    "falconx": "FalconX trading data",
    "sigma": "Sigma computing dashboards",
    "messari": "Messari crypto research data",
    "idxs": "Blockchain data (transfers, txs, events)",
    "similarweb": "Web traffic, rankings, keywords",
    "nansen": "Nansen on-chain analytics",
}

# Max output size to avoid blowing up context
MAX_OUTPUT_BYTES = 50_000


async def _run(cmd: list[str], timeout: int = 60) -> str:
    """Run a CLI command and return stdout, or error message."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return json.dumps({"error": f"Command timed out after {timeout}s", "command": cmd})

    output = stdout.decode("utf-8", errors="replace")
    if proc.returncode != 0:
        err = stderr.decode("utf-8", errors="replace")
        return json.dumps({
            "error": f"Command exited with code {proc.returncode}",
            "stderr": err[:2000],
            "stdout": output[:2000],
        })

    if len(output) > MAX_OUTPUT_BYTES:
        output = output[:MAX_OUTPUT_BYTES] + f"\n... (truncated, {len(stdout)} bytes total)"

    return output


def _resolve_bin(cli: str) -> str | None:
    """Find the CLI binary path."""
    # Check uv tool bin first, then PATH
    import os

    uv_path = f"{_BIN}/{cli}"
    if os.path.isfile(uv_path):
        return uv_path
    return shutil.which(cli)


async def run_cli(cli: str, args: list[str], timeout: int = 60) -> str:
    """Run an allowed CLI with given arguments."""
    if cli not in ALLOWED_CLIS:
        return json.dumps({
            "error": f"CLI '{cli}' not allowed",
            "allowed": list(ALLOWED_CLIS.keys()),
        })

    bin_path = _resolve_bin(cli)
    if not bin_path:
        return json.dumps({"error": f"CLI '{cli}' not found on this system"})

    return await _run([bin_path, *args], timeout=timeout)
