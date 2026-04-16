"""Workflow: monitor curated RSS feeds for policy-relevant news and post to Slack."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.policy_news import DEFAULT_POLICY_NEWS_FEEDS_FILE, normalize_slack_channel, run_policy_news_monitor
from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "policy_news_monitor"


def _configured_feeds_file() -> str:
    return os.getenv("POLICY_NEWS_FEEDS_FILE", "").strip() or DEFAULT_POLICY_NEWS_FEEDS_FILE


def _load_file_config(feeds_file: str) -> dict[str, Any]:
    if not feeds_file:
        return {}
    path = Path(feeds_file)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _configured_slack_channel(file_config: dict[str, Any]) -> str:
    return normalize_slack_channel(
        os.getenv("POLICY_NEWS_SLACK_CHANNEL", "").strip()
        or str(file_config.get("slack_channel") or "").strip()
    )


def _configured_enabled(feeds_file: str, file_config: dict[str, Any]) -> bool:
    enabled_override = os.getenv("POLICY_NEWS_ENABLED", "").strip().lower()
    if enabled_override:
        return enabled_override in {"1", "true", "yes"}
    return bool(feeds_file and file_config.get("sources") and _configured_slack_channel(file_config))


_FEEDS_FILE = _configured_feeds_file()
_FILE_CONFIG = _load_file_config(_FEEDS_FILE)

SCHEDULE = {
    "interval_seconds": 900,
    "slack_channel": _configured_slack_channel(_FILE_CONFIG),
    "enabled": _configured_enabled(_FEEDS_FILE, _FILE_CONFIG),
    "input": {
        "feeds_file": _FEEDS_FILE,
    },
}


@dataclass
class Input:
    slack_channel: str = ""
    feeds_file: str = ""
    sources: list[dict[str, Any]] = field(default_factory=list)
    dry_run: bool = False
    process_replies: bool = True
    max_articles_per_feed: int = 40
    max_candidates: int = 40
    max_alerts_per_run: int = 8
    max_query_results: int = 8
    classifier_provider: str = "auto"
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    return await run_policy_news_monitor(ctx, inp)
