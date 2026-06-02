#!/usr/bin/env python3
"""Refresh Slack thread readbacks for local fuzz cases and extract user feedback."""

from __future__ import annotations

import argparse
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_CORPUS_DIR = Path("local-corpus/slackbot-fuzz")


def main() -> int:
    args = parse_args()
    corpus_dir = Path(args.corpus_dir)
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    output_dir = corpus_dir / f"feedback-refresh-{run_id}"
    output_dir.mkdir(parents=True, exist_ok=True)

    cases = discover_slack_cases(corpus_dir)
    summaries = []
    for case in cases:
        dump_path = output_dir / f"{safe_name(case['case_path'])}.json"
        channel, ts = parse_thread_key(case["thread_key"])
        subprocess.run(
            ["./slack", "thread", f"{channel}:{ts}", "--compact", "--output", str(dump_path)],
            check=True,
            timeout=args.timeout_s,
        )
        dump = json.loads(dump_path.read_text(encoding="utf-8"))
        summaries.append(summarize_thread(case, dump, dump_path))

    output = {
        "schema": "centaur.slackbot_fuzz_feedback_summary.v1",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "case_count": len(cases),
        "threads_with_post_bot_user_messages": sum(
            1 for summary in summaries if summary["post_bot_user_messages"]
        ),
        "feedback_like_count": sum(
            len(summary["feedback_like_messages"]) for summary in summaries
        ),
        "threads": summaries,
    }
    summary_path = output_dir / "summary.json"
    summary_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(summary_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", default=str(DEFAULT_CORPUS_DIR))
    parser.add_argument("--run-id")
    parser.add_argument("--timeout-s", type=float, default=180)
    return parser.parse_args()


def discover_slack_cases(corpus_dir: Path) -> list[dict[str, Any]]:
    cases = []
    for path in sorted(corpus_dir.glob("**/case.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        thread_key = str(case.get("thread_key") or "")
        if not thread_key.startswith("slack:"):
            continue
        cases.append(
            {
                "case_path": str(path),
                "case_id": str(case.get("case_id") or path.parent.name),
                "surface": str(case.get("surface") or ""),
                "thread_key": thread_key,
                "issues": list(case.get("issues") or []),
            }
        )
    return cases


def parse_thread_key(thread_key: str) -> tuple[str, str]:
    parts = thread_key.split(":", 2)
    if len(parts) != 3 or parts[0] != "slack":
        raise ValueError(f"not a slack thread key: {thread_key}")
    return parts[1], parts[2]


def summarize_thread(case: dict[str, Any], dump: dict[str, Any], dump_path: Path) -> dict[str, Any]:
    messages = list(dump.get("messages") or [])
    auth = dump.get("auth") if isinstance(dump.get("auth"), dict) else {}
    user_id = str(auth.get("user_id") or "")
    first_assistant_index = next(
        (index for index, message in enumerate(messages) if is_centaur_bot_message(message)),
        None,
    )
    post_bot_user_messages = []
    feedback_like_messages = []
    if first_assistant_index is not None:
        for message in messages[first_assistant_index + 1 :]:
            if not is_user_feedback_candidate(message, user_id):
                continue
            entry = {
                "ts": str(message.get("ts") or ""),
                "user": str(message.get("user") or ""),
                "text": str(message.get("text") or ""),
            }
            post_bot_user_messages.append(entry)
            if is_feedback_like(entry["text"]):
                feedback_like_messages.append(entry)

    return {
        **case,
        "dump_path": str(dump_path),
        "message_count": len(messages),
        "bot_message_count": sum(1 for message in messages if is_centaur_bot_message(message)),
        "first_assistant_ts": (
            str(messages[first_assistant_index].get("ts") or "")
            if first_assistant_index is not None
            else None
        ),
        "post_bot_user_messages": post_bot_user_messages,
        "feedback_like_messages": feedback_like_messages,
    }


def is_centaur_bot_message(message: dict[str, Any]) -> bool:
    user = str(message.get("user") or "")
    text = str(message.get("text") or "")
    return (
        user in {"U0B7CFP79PF", "U0ANX3AM5RR", "U0AAA8F0JEM"}
        or "Centaur" in text
        or "This message contains interactive elements." in text
    ) and not str(message.get("bot_id") or "").startswith("B0B78")


def is_user_feedback_candidate(message: dict[str, Any], user_id: str) -> bool:
    if message.get("bot_id"):
        return False
    if user_id and str(message.get("user") or "") != user_id:
        return False
    return bool(str(message.get("text") or "").strip())


def is_feedback_like(text: str) -> bool:
    lower = text.lower()
    markers = (
        "observe",
        "should",
        "shouldn't",
        "dont",
        "don't",
        "right ux",
        "wrong",
        "looks",
        "ideally",
        "idk",
        "feedback",
        "not",
    )
    return any(marker in lower for marker in markers)


def safe_name(path: str) -> str:
    normalized = path.removeprefix("local-corpus/slackbot-fuzz/")
    return (
        normalized.replace("/", "__")
        .replace(":", "_")
        .replace(" ", "_")
        .removesuffix("__case.json")
        .removesuffix(".json")
    )


if __name__ == "__main__":
    raise SystemExit(main())
