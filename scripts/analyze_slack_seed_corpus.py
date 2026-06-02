#!/usr/bin/env python3
"""Summarize saved Slack thread seed dumps for Slackbot regression fixtures."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


NUDGE_PATTERNS = (
    "so?",
    "bump",
    "still working",
    "what happened",
    "finish",
    "u up",
    "broke",
    "lagging",
    "wrong",
    "slop",
    "continue",
)


def main() -> int:
    args = parse_args()
    seed_dir = Path(args.seed_dir)
    summaries = [summarize_file(path) for path in sorted(seed_dir.glob("*.json"))]
    output = {
        "schema": "centaur.slackbot_seed_summary.v1",
        "seed_dir": str(seed_dir),
        "seed_count": len(summaries),
        "issue_counts": issue_counts(summaries),
        "seeds": summaries,
    }
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("seed_dir")
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def summarize_file(path: Path) -> dict[str, Any]:
    dump = json.loads(path.read_text(encoding="utf-8"))
    messages = list(dump.get("messages") or [])
    bot_messages = [message for message in messages if is_bot_message(message)]
    user_messages = [message for message in messages if not is_bot_message(message)]

    raw_interactive = []
    empty_bot = []
    visible_thinking = []
    tiny_fragments = []
    plan_only = []
    task_errors = []
    assistant_bursts = []
    user_nudges = []
    plan_titles: list[str] = []
    no_rollout_found = []
    runtime_start_failures = []
    request_cancelled = []
    agent_request_failed_before_start = []
    stream_without_assistant = []
    runtime_issue_messages = []

    previous_bot_final: dict[str, Any] | None = None
    for message in messages:
        text = str(message.get("text") or "")
        ts = str(message.get("ts") or "")
        blocks = list(message.get("blocks") or [])
        plans = [block for block in blocks if block.get("type") == "plan"]

        if is_bot_message(message):
            non_plan_text = "\n".join(
                text_from_block(block).strip() for block in blocks if block.get("type") != "plan"
            ).strip()
            final_text = non_plan_text if meaningful_final_text(non_plan_text) else ""
            if not final_text and meaningful_final_text(text):
                final_text = text

            if text == "This message contains interactive elements.":
                raw_interactive.append(ts)
            if "no rollout found for thread id" in text.lower():
                no_rollout_found.append({"ts": ts, "text": text})
            if "failed to start the runtime" in text.lower() or "sandbox readiness timed out" in text.lower():
                runtime_start_failures.append({"ts": ts, "text": text})
            if "request cancelled" in text.lower():
                request_cancelled.append({"ts": ts, "text": text})
            if "agent request failed before execution started" in text.lower():
                agent_request_failed_before_start.append({"ts": ts, "text": text})
            if "stream ended without producing a message with role=assistant" in text.lower():
                stream_without_assistant.append({"ts": ts, "text": text})
            if "agent hit a runtime issue before finishing" in text.lower():
                runtime_issue_messages.append({"ts": ts, "text": text})
            if not text.strip() and not blocks:
                empty_bot.append(ts)
            if text.lstrip().startswith("*Thinking*"):
                visible_thinking.append(ts)
            if looks_like_tiny_fragment(text):
                tiny_fragments.append({"ts": ts, "text": text})
            if plans and not final_text:
                plan_only.append(ts)
            for plan in plans:
                title = str(plan.get("title") or "")
                if title:
                    plan_titles.append(title)
                for task in plan.get("tasks") or []:
                    if str(task.get("status") or "").lower() == "error":
                        task_errors.append({"ts": ts, "title": str(task.get("title") or "")})

            if final_text:
                if previous_bot_final and seconds_between(previous_bot_final["ts"], ts) <= 20:
                    assistant_bursts.append(
                        {
                            "first_ts": previous_bot_final["ts"],
                            "second_ts": ts,
                            "first_preview": previous_bot_final["text"][:160],
                            "second_preview": final_text[:160],
                        }
                    )
                previous_bot_final = {"ts": ts, "text": final_text}
        else:
            lower = text.lower()
            matched = [pattern for pattern in NUDGE_PATTERNS if pattern in lower]
            if matched:
                user_nudges.append({"ts": ts, "patterns": matched, "text_preview": text[:180]})

    issues: list[str] = []
    if raw_interactive:
        issues.append("raw_interactive_elements_fallback_text")
    if empty_bot:
        issues.append("empty_bot_message")
    if visible_thinking:
        issues.append("visible_thinking_text")
    if tiny_fragments:
        issues.append("tiny_fragment_message")
    if plan_only and not any_meaningful_bot_final(bot_messages):
        issues.append("plan_only_thread_without_final")
    if task_errors:
        issues.append("plan_task_error")
    if assistant_bursts:
        issues.append("assistant_duplicate_or_burst_final")
    if user_nudges:
        issues.append("user_nudge_after_poor_or_slow_response")
    if no_rollout_found:
        issues.append("no_rollout_found_message")
    if runtime_start_failures:
        issues.append("runtime_start_failure_visible_text")
    if request_cancelled:
        issues.append("request_cancelled_message")
    if agent_request_failed_before_start:
        issues.append("agent_request_failed_before_start")
    if stream_without_assistant:
        issues.append("stream_without_assistant_message")
    if runtime_issue_messages:
        issues.append("runtime_issue_visible_text")

    return {
        "file": str(path),
        "thread_key": thread_key_from_dump(dump),
        "message_count": len(messages),
        "bot_message_count": len(bot_messages),
        "user_message_count": len(user_messages),
        "issues": sorted(set(issues)),
        "raw_interactive_elements_ts": raw_interactive,
        "empty_bot_message_ts": empty_bot,
        "visible_thinking_ts": visible_thinking,
        "tiny_fragments": tiny_fragments,
        "plan_only_ts": plan_only,
        "task_errors": task_errors,
        "assistant_bursts": assistant_bursts,
        "user_nudges": user_nudges,
        "no_rollout_found": no_rollout_found,
        "runtime_start_failures": runtime_start_failures,
        "request_cancelled": request_cancelled,
        "agent_request_failed_before_start": agent_request_failed_before_start,
        "stream_without_assistant": stream_without_assistant,
        "runtime_issue_messages": runtime_issue_messages,
        "plan_titles": sorted(set(plan_titles)),
    }


def issue_counts(summaries: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for summary in summaries:
        for issue in summary["issues"]:
            counts[issue] = counts.get(issue, 0) + 1
    return dict(sorted(counts.items()))


def thread_key_from_dump(dump: dict[str, Any]) -> str:
    channel = str(dump.get("channel_id") or dump.get("channel") or "")
    messages = list(dump.get("messages") or [])
    ts = str((messages[0] if messages else {}).get("thread_ts") or (messages[0] if messages else {}).get("ts") or "")
    return f"slack:{channel}:{ts}" if channel and ts else ""


def is_bot_message(message: dict[str, Any]) -> bool:
    return bool(message.get("bot_id")) or str(message.get("user") or "").startswith("U0ANX")


def seconds_between(first_ts: str, second_ts: str) -> float:
    try:
        return abs(float(second_ts) - float(first_ts))
    except ValueError:
        return 999999.0


def any_meaningful_bot_final(messages: list[dict[str, Any]]) -> bool:
    for message in messages:
        text = str(message.get("text") or "")
        blocks = list(message.get("blocks") or [])
        non_plan_text = "\n".join(
            text_from_block(block).strip() for block in blocks if block.get("type") != "plan"
        ).strip()
        if meaningful_final_text(non_plan_text) or meaningful_final_text(text):
            return True
    return False


def meaningful_final_text(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    ignored = {
        "This message contains interactive elements.",
        "_base · codex_, with interactive elements",
        "base · codex",
    }
    if normalized in ignored:
        return False
    if normalized.endswith(", with interactive elements") and len(normalized) < 100:
        prefix = normalized.removesuffix(", with interactive elements").strip("_ ")
        return bool(prefix and prefix not in {"base · codex", "base · centaur"})
    return normalized.strip("_ ") not in {"base · codex", "base · centaur"}


def looks_like_tiny_fragment(text: str) -> bool:
    normalized = text.strip()
    if not normalized or len(normalized) > 12:
        return False
    if re.search(r"[A-Za-z0-9]{3,}", normalized):
        return False
    return bool(re.search(r"[?*_`\"'“”‘’.]", normalized))


def text_from_block(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        return "".join(text_from_block(item) for item in block)
    if not isinstance(block, dict):
        return ""
    parts: list[str] = []
    if isinstance(block.get("text"), str):
        parts.append(block["text"])
    for key in ("elements", "blocks", "fields"):
        value = block.get(key)
        if isinstance(value, list):
            parts.append("".join(text_from_block(item) for item in value))
    return "".join(parts)


if __name__ == "__main__":
    raise SystemExit(main())
