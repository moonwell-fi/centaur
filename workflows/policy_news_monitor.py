"""Workflow: monitor curated RSS feeds for policy-relevant news and post to Slack."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

from api.policy_news import run_policy_news_monitor
from api.workflow_engine import WorkflowContext

WORKFLOW_NAME = "policy_news_monitor"
SCHEDULE = {
    "interval_seconds": 900,
    "slack_channel": os.getenv("POLICY_NEWS_SLACK_CHANNEL", "").strip(),
    "enabled": os.getenv("POLICY_NEWS_ENABLED", "").strip().lower()
    in {"1", "true", "yes"},
    "input": {
        "feeds_file": os.getenv("POLICY_NEWS_FEEDS_FILE", "").strip(),
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
