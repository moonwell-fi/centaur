"""Workflow: autonomous nightly self-improvement loop.

Internal modes:
- parent: the scheduled nightly review and fix-selection pass
- fix_child: one focused child run for one selected fix
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from api.runtime_control import ControlPlaneError, decode_jsonb
from api.workflow_engine import (
    CancelledWorkflow,
    Delivery,
    NonRetryableError,
    SuspendWorkflow,
    WorkflowContext,
)
from workflows.json_payloads import extract_json_payload, has_required_keys, missing_required_keys

WORKFLOW_NAME = "self_improve_daily"
SCHEDULE = {
    "cron": "0 22 * * *",
    "timezone": "America/Los_Angeles",
    "slack_channel": "ai-agent",
    "catchup_policy": "skip",
}

PRIOR_CONTEXT_WINDOW = 3
FOLLOWUP_LIMIT = 8
FOLLOWUP_CUTOFF_HOURS = 4
CHILD_TIMEOUT_HOURS = 2
DEDUP_WINDOW_HOURS = 72
MAX_DELIVERY_TEXT_CHARS = 2000

TRIAGE_PREFERRED_KEYS = ("selected_task_ids", "task_assessments")
TRIAGE_REQUIRED_KEYS = ("selected_task_ids",)
RECONCILE_PREFERRED_KEYS = ("reconciled_fixes",)
RECONCILE_REQUIRED_KEYS = ("reconciled_fixes",)
_GITHUB_REPO_RE = re.compile(r"github\.com[:/](?P<repo>[^/]+/[^/.]+?)(?:\.git)?$")


def _normalize_github_repo(value: str | None) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    match = _GITHUB_REPO_RE.search(raw)
    if match:
        return match.group("repo")
    return raw.strip("/")


def _resolve_current_repo() -> str:
    for key in ("SELF_IMPROVE_REPO", "GITHUB_REPOSITORY"):
        repo = _normalize_github_repo(os.getenv(key))
        if repo:
            return repo
    try:
        origin = subprocess.check_output(
            ["git", "config", "--get", "remote.origin.url"],
            cwd=Path(__file__).resolve().parents[1],
            text=True,
        ).strip()
    except Exception:
        return ""
    return _normalize_github_repo(origin)


def _env_positive_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(value, 1)


def _env_nonnegative_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError:
        value = default
    return max(value, 0)


REVIEW_WINDOW_HOURS_DEFAULT = _env_positive_int("SELF_IMPROVE_REVIEW_WINDOW_HOURS", 24)
MAX_SELECTED_FIXES_DEFAULT = _env_nonnegative_int("SELF_IMPROVE_MAX_SELECTED_FIXES", 0)
CANDIDATE_LIMIT_DEFAULT = _env_nonnegative_int("SELF_IMPROVE_CANDIDATE_LIMIT", 0)
REVIEW_BATCH_SIZE_DEFAULT = _env_positive_int("SELF_IMPROVE_REVIEW_BATCH_SIZE", 10)
CANDIDATE_FETCH_FACTOR = _env_positive_int("SELF_IMPROVE_CANDIDATE_FETCH_FACTOR", 4)
REVIEW_PREFERRED_KEYS = (
    "tasks_reviewed",
    "below_bar_count",
    "below_bar_rate",
    "task_reviews",
    "top_failure_modes",
    "selected_fixes",
)
REVIEW_REQUIRED_KEYS = ("task_reviews", "selected_fixes")
SYNTHESIS_PREFERRED_KEYS = (
    "sessions_analyzed",
    "opportunities_found",
    "opportunities",
    "selected_builds",
)
SYNTHESIS_REQUIRED_KEYS = ("opportunities", "selected_builds")
EXECUTE_PREFERRED_KEYS = (
    "branch",
    "commit",
    "pr_number",
    "pr_url",
    "pr_title",
    "verified_handoff",
    "research",
    "plan",
    "changed_files",
    "validation",
)
EXECUTE_REQUIRED_KEYS = ("branch", "pr_number", "pr_url")


@dataclass
class Input:
    mode: str = "parent"
    review_window_hours: int = REVIEW_WINDOW_HOURS_DEFAULT
    max_selected_fixes: int = MAX_SELECTED_FIXES_DEFAULT
    candidate_limit: int = CANDIDATE_LIMIT_DEFAULT
    review_batch_size: int = REVIEW_BATCH_SIZE_DEFAULT
    fix_packet: dict[str, Any] = field(default_factory=dict)


def _message_text(parts: list[dict[str, Any]]) -> str:
    texts = [
        str(part.get("text") or "").strip()
        for part in parts
        if isinstance(part, dict) and part.get("type") == "text"
    ]
    return "\n".join(text for text in texts if text).strip()


def _message_part_types(parts: list[dict[str, Any]]) -> list[str]:
    return sorted(
        {
            str(part.get("type") or "").strip()
            for part in parts
            if isinstance(part, dict) and str(part.get("type") or "").strip()
        }
    )


def _extract_required_json_payload(
    text: str,
    *,
    stage: str,
    preferred_keys: tuple[str, ...] = (),
    required_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    payload = extract_json_payload(text, preferred_keys=preferred_keys)
    if required_keys and not has_required_keys(payload, required_keys):
        missing = ", ".join(missing_required_keys(payload, required_keys))
        payload_keys = ", ".join(sorted(payload.keys()))
        snippet = str(payload.get("raw_snippet") or "")[:160]
        raise RuntimeError(
            f"{stage} response missing required keys [{missing}] "
            f"(payload keys: [{payload_keys}]; snippet: {snippet})"
        )
    return payload


def _parse_thread_key(thread_key: str) -> tuple[str, str]:
    parts = thread_key.strip().split(":")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[1] and parts[2]:
        return parts[1], parts[2]
    raise ValueError(f"invalid thread key: {thread_key}")


def _slack_ts_to_datetime(value: str) -> dt.datetime | None:
    seconds, _, micros = value.partition(".")
    if not seconds:
        return None
    try:
        base = int(seconds)
        frac = float(f"0.{micros}") if micros else 0.0
    except ValueError:
        return None
    return dt.datetime.fromtimestamp(base + frac, tz=dt.timezone.utc)


def _normalize_message(row: dict[str, Any]) -> dict[str, Any]:
    parts = decode_jsonb(row.get("parts"), [])
    metadata = decode_jsonb(row.get("metadata"), {})
    part_list = parts if isinstance(parts, list) else []
    return {
        "id": str(row.get("id") or ""),
        "role": str(row.get("role") or "user"),
        "parts": part_list,
        "metadata": metadata if isinstance(metadata, dict) else {},
        "created_at": row.get("created_at"),
        "text": _message_text(part_list),
        "part_types": _message_part_types(part_list),
    }


def _message_user_display(
    message: dict[str, Any],
    user_name_by_id: dict[str, str] | None = None,
) -> str:
    """Resolve a Slack-side display name for *message*.

    In production, ``chat_messages.metadata`` only carries ``user_id`` —
    the slackbot does not resolve names at insert time. We therefore take
    an optional ``user_name_by_id`` cache (typically fetched once per run
    via the Slack tool) and use it to hydrate the name. Explicit fields
    on the metadata still win when present so local tests and future
    slackbot changes that populate names directly keep working.
    """
    metadata = message.get("metadata") or {}
    if not isinstance(metadata, dict):
        return ""
    direct = str(
        metadata.get("user_name")
        or metadata.get("name")
        or metadata.get("username")
        or ""
    ).strip()
    if direct:
        return direct
    if user_name_by_id:
        user_id = str(metadata.get("user_id") or "").strip()
        if user_id:
            resolved = str(user_name_by_id.get(user_id) or "").strip()
            if resolved:
                return resolved
    return ""


def _first_name_from_user_name(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    cleaned = re.sub(r"\s+", " ", raw).strip()
    if not cleaned:
        return ""
    first = re.split(r"[\s._/()]+", cleaned, maxsplit=1)[0].strip("'\"<>{}[]")
    return first


def _message_user_id(message: dict[str, Any]) -> str:
    metadata = message.get("metadata") or {}
    if not isinstance(metadata, dict):
        return ""
    return str(metadata.get("user_id") or "").strip()


def _slack_user_mention(user_id: str) -> str:
    cleaned = str(user_id or "").strip()
    return f"<@{cleaned}>" if cleaned else ""


def _serialize_messages(
    messages: list[dict[str, Any]],
    user_name_by_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for message in messages:
        created_at = message.get("created_at")
        payload.append(
            {
                "message_id": message.get("metadata", {}).get("message_id"),
                "role": message.get("role"),
                "created_at": created_at.isoformat()
                if isinstance(created_at, dt.datetime)
                else None,
                "text": message.get("text", ""),
                "part_types": list(message.get("part_types") or []),
                "user_name": _message_user_display(message, user_name_by_id),
            }
        )
    return payload


def _summarize_followups(messages: list[dict[str, Any]]) -> dict[str, Any]:
    texts = [str(message.get("text") or "").strip() for message in messages]
    return {
        "followup_count": len(messages),
        "has_followup": bool(messages),
        "example_texts": [text for text in texts if text][:5],
    }


def _validate_source_user_names(
    tasks: list[dict[str, Any]],
    *,
    user_name_by_id: dict[str, str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    cache = user_name_by_id or {}
    normalized_tasks: list[dict[str, Any]] = []
    hydrated_count = 0
    normalized_count = 0
    unresolved_user_ids: list[str] = []

    for task in tasks:
        if not isinstance(task, dict):
            continue
        normalized = dict(task)
        user_id = str(task.get("source_user_id") or "").strip()
        original_name = str(task.get("source_user_name") or "").strip()
        first_name = _first_name_from_user_name(original_name)
        if first_name and first_name != original_name:
            normalized_count += 1
        if not first_name and user_id:
            first_name = _first_name_from_user_name(cache.get(user_id, ""))
            if first_name:
                hydrated_count += 1
            else:
                unresolved_user_ids.append(user_id)
        normalized["source_user_name"] = first_name
        normalized_tasks.append(normalized)

    unique_unresolved_ids = list(dict.fromkeys(unresolved_user_ids))
    missing_name_count = sum(
        1
        for task in normalized_tasks
        if str(task.get("source_user_id") or "").strip()
        and not str(task.get("source_user_name") or "").strip()
    )
    return normalized_tasks, {
        "hydrated_count": hydrated_count,
        "normalized_count": normalized_count,
        "missing_name_count": missing_name_count,
        "unresolved_user_ids": unique_unresolved_ids,
        "complete": missing_name_count == 0,
    }


def _sum_int_values(mapping: Any) -> int:
    if not isinstance(mapping, dict):
        return 0
    total = 0
    for value in mapping.values():
        try:
            total += int(value or 0)
        except (TypeError, ValueError):
            continue
    return total


def _sum_mapping_values(items: list[dict[str, Any]], key: str) -> dict[str, int]:
    totals: dict[str, int] = {}
    for item in items:
        mapping = item.get(key)
        if not isinstance(mapping, dict):
            continue
        for map_key, value in mapping.items():
            try:
                totals[str(map_key)] = totals.get(str(map_key), 0) + int(value or 0)
            except (TypeError, ValueError):
                continue
    return totals


def _compact_task_summary(task: dict[str, Any]) -> dict[str, Any]:
    followup_summary = task.get("user_followup_summary") or {}
    return {
        "task_id": task.get("task_id"),
        "thread_key": task.get("thread_key"),
        "source_user_mention": str(task.get("source_user_mention") or ""),
        "source_user_name": str(task.get("source_user_name") or ""),
        "ask_text": str(task.get("ask_text") or "")[:500],
        "status": task.get("status"),
        "terminal_reason": task.get("terminal_reason"),
        "duration_s": task.get("duration_s"),
        "tool_errors": _sum_int_values(task.get("tool_errors_by_name")),
        "tool_retry_count": task.get("tool_retry_count", 0),
        "subagent_failures": task.get("subagent_failures", 0),
        "command_error_events": task.get("command_error_events", 0),
        "followup_count": followup_summary.get("followup_count", 0),
        "followup_texts": followup_summary.get("example_texts", []),
        "delivery_snippet": str(task.get("final_delivery_text") or "")[:300],
    }


_RELATED_WINDOW_STOP_WORDS = {
    "add",
    "for",
    "from",
    "into",
    "that",
    "this",
    "with",
    "your",
    "what",
    "when",
    "where",
    "which",
    "workflow",
    "system",
    "persona",
    "skill",
    "tool",
    "improvement",
}


def _keywords_for_related_search(text: str) -> set[str]:
    tokens = {
        token.strip().lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_-]{3,}", str(text or ""))
    }
    return {token for token in tokens if token not in _RELATED_WINDOW_STOP_WORDS}


def _augment_builds_with_related_window_threads(
    selected_builds: list[dict[str, Any]],
    all_tasks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    task_by_thread = {
        str(task.get("thread_key") or "").strip(): task
        for task in all_tasks
        if isinstance(task, dict) and str(task.get("thread_key") or "").strip()
    }
    augmented: list[dict[str, Any]] = []
    for build in selected_builds:
        if not isinstance(build, dict):
            continue
        enriched = dict(build)
        source_threads = _normalize_source_threads(build.get("source_threads"))
        source_thread_keys = {
            str(item.get("thread_key") or "").strip()
            for item in source_threads
            if isinstance(item, dict)
        }
        source_user_ids = {
            str(task_by_thread[key].get("source_user_id") or "").strip()
            for key in source_thread_keys
            if key in task_by_thread and str(task_by_thread[key].get("source_user_id") or "").strip()
        }
        source_user_names = {
            str(task_by_thread[key].get("source_user_name") or "").strip()
            for key in source_thread_keys
            if key in task_by_thread and str(task_by_thread[key].get("source_user_name") or "").strip()
        }
        keywords = _keywords_for_related_search(
            f"{build.get('title', '')} {build.get('implementation_sketch', '')} {build.get('evidence_summary', '')}"
        )
        related_threads: list[dict[str, str]] = []
        seen_related: set[str] = set()
        for task in all_tasks:
            if not isinstance(task, dict):
                continue
            thread_key = str(task.get("thread_key") or "").strip()
            if not thread_key or thread_key in source_thread_keys or thread_key in seen_related:
                continue
            source_user_id = str(task.get("source_user_id") or "").strip()
            source_user_name = str(task.get("source_user_name") or "").strip()
            ask_text = str(task.get("ask_text") or "").casefold()
            has_keyword_overlap = bool(keywords) and any(keyword in ask_text for keyword in keywords)
            if (
                (source_user_id and source_user_id in source_user_ids)
                or (source_user_name and source_user_name in source_user_names)
                or has_keyword_overlap
            ):
                seen_related.add(thread_key)
                related_threads.append(
                    {
                        "thread_key": thread_key,
                        "channel": str(task.get("channel") or "").strip(),
                        "thread_ts": str(task.get("thread_ts") or "").strip(),
                    }
                )
            if len(related_threads) >= 5:
                break
        enriched["related_window_threads"] = related_threads
        if related_threads:
            combined_threads = _normalize_source_threads(source_threads + related_threads)
            enriched["source_threads"] = combined_threads
            enriched["evidence_summary"] = (
                f"{str(build.get('evidence_summary') or '').strip()} "
                f"Related same-day threads in the review window: {len(related_threads)}."
            ).strip()
        augmented.append(enriched)
    return augmented


async def _run_triage_pass(
    ctx: WorkflowContext,
    *,
    tasks: list[dict[str, Any]],
    limit: int,
) -> list[str]:
    if not tasks:
        return []
    if len(tasks) <= limit:
        return [str(task.get("task_id") or "") for task in tasks]

    summaries = [_compact_task_summary(task) for task in tasks]
    prompt = textwrap.dedent(
        f"""
        You are triaging Slack-thread user tasks for a nightly self-improvement review.

        Below are {len(summaries)} reconstructed tasks from the past day. Select the
        {limit} most valuable tasks to send to the full quality-review pass.

        Selection criteria (use your judgment, not rigid rules):
        - Prioritize tasks where the user likely had a bad experience: failed
          executions, negative or corrective follow-ups, timeouts, silence.
        - Include a mix of failure modes so the review covers diverse issues.
        - Include at least 1-2 tasks that completed successfully so the reviewer
          can calibrate what "good" looks like in this batch.
        - Interpret follow-up messages semantically. "thanks" after a good answer
          is positive. "can you try again" after a failure is negative. Do not
          rely on keyword matching — read the actual conversation snippets.
        - Consider execution telemetry (errors, retries, duration) as supporting
          evidence, not as the sole selection criterion.

        Return JSON only with exactly these top-level keys:
        - `selected_task_ids`: array of {limit} task_id strings, ordered by
          review priority (most important first).
        - `task_assessments`: array of objects, one per input task, each with
          `task_id`, `review_priority` ("high"/"medium"/"low"),
          `followup_quality` ("positive"/"neutral"/"negative"/"none"),
          and a short `rationale`.

        Task summaries:
        ```json
        {json.dumps(summaries, indent=2)}
        ```
        """
    ).strip()

    triage_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:triage",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "triage",
        },
    )

    async def _parse_triage() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(triage_turn.get("result_text") or ""),
            stage="triage",
            preferred_keys=TRIAGE_PREFERRED_KEYS,
            required_keys=TRIAGE_REQUIRED_KEYS,
        )

    triage_result = await ctx.step("triage_tasks", _parse_triage, step_kind="review")
    selected_ids = list(triage_result.get("selected_task_ids") or [])
    ctx.log(
        "self_improve_triage_completed",
        candidate_count=len(tasks),
        selected_count=len(selected_ids),
    )
    return [str(tid) for tid in selected_ids[:limit]]


def _looks_insufficient(task: dict[str, Any]) -> bool:
    return not str(task.get("ask_text") or "").strip()


def _normalize_source_threads(items: Any) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    if not isinstance(items, list):
        return normalized
    for item in items:
        if not isinstance(item, dict):
            continue
        thread_key = str(item.get("thread_key") or "").strip()
        channel = str(item.get("channel") or "").strip()
        thread_ts = str(item.get("thread_ts") or "").strip()
        if not channel and not thread_ts and thread_key:
            try:
                channel, thread_ts = _parse_thread_key(thread_key)
            except ValueError:
                continue
        if not thread_key and channel and thread_ts:
            thread_key = f"{channel}:{thread_ts}"
        if thread_key and channel and thread_ts:
            normalized.append(
                {
                    "thread_key": thread_key,
                    "channel": channel,
                    "thread_ts": thread_ts,
                }
            )
    return normalized


def _selection_limit(requested: int, available: int) -> int:
    if requested <= 0:
        return max(available, 1)
    return max(requested, 1)


def _chunk_tasks(tasks: list[dict[str, Any]], chunk_size: int) -> list[list[dict[str, Any]]]:
    size = max(chunk_size, 1)
    return [tasks[index:index + size] for index in range(0, len(tasks), size)]


def _merge_review_batches(batch_reviews: list[dict[str, Any]], *, tasks_reviewed: int) -> dict[str, Any]:
    merged = _empty_review(tasks_reviewed)
    task_reviews: list[dict[str, Any]] = []
    selected_fixes: list[dict[str, Any]] = []
    failure_modes: dict[str, dict[str, Any]] = {}
    below_bar_count = 0

    for review in batch_reviews:
        if not isinstance(review, dict):
            continue
        below_bar_count += int(review.get("below_bar_count") or 0)
        for task in list(review.get("task_reviews") or []):
            if isinstance(task, dict):
                task_reviews.append(task)
        for fix in list(review.get("selected_fixes") or []):
            if isinstance(fix, dict):
                selected_fixes.append(fix)
        for entry in list(review.get("top_failure_modes") or []):
            if not isinstance(entry, dict):
                continue
            key = str(entry.get("failure_mode") or "").strip()
            if not key:
                continue
            bucket = failure_modes.setdefault(
                key,
                {
                    "failure_mode": key,
                    "count": 0,
                    "representative_threads": [],
                },
            )
            bucket["count"] += int(entry.get("count") or 0)
            seen = set(bucket["representative_threads"])
            for thread_key in list(entry.get("representative_threads") or []):
                candidate = str(thread_key or "").strip()
                if candidate and candidate not in seen:
                    bucket["representative_threads"].append(candidate)
                    seen.add(candidate)

    merged["task_reviews"] = task_reviews
    merged["selected_fixes"] = selected_fixes
    merged["below_bar_count"] = below_bar_count
    merged["below_bar_rate"] = (
        float(below_bar_count) / float(tasks_reviewed) if tasks_reviewed else 0.0
    )
    merged["top_failure_modes"] = sorted(
        failure_modes.values(),
        key=lambda item: (-int(item.get("count") or 0), str(item.get("failure_mode") or "")),
    )[:10]
    return merged


def _empty_review(tasks_reviewed: int) -> dict[str, Any]:
    return {
        "tasks_reviewed": tasks_reviewed,
        "below_bar_count": 0,
        "below_bar_rate": 0.0,
        "task_reviews": [],
        "top_failure_modes": [],
        "selected_fixes": [],
    }


def _normalize_review(review: dict[str, Any], *, tasks_reviewed: int) -> dict[str, Any]:
    normalized = _empty_review(tasks_reviewed)
    if not isinstance(review, dict):
        return normalized
    normalized.update(review)
    normalized["tasks_reviewed"] = int(review.get("tasks_reviewed") or tasks_reviewed)
    normalized["below_bar_count"] = int(review.get("below_bar_count") or 0)
    try:
        normalized["below_bar_rate"] = float(review.get("below_bar_rate") or 0.0)
    except (TypeError, ValueError):
        normalized["below_bar_rate"] = 0.0
    normalized["task_reviews"] = list(review.get("task_reviews") or [])
    normalized["top_failure_modes"] = list(review.get("top_failure_modes") or [])
    fixes = []
    for item in list(review.get("selected_fixes") or []):
        if not isinstance(item, dict):
            continue
        fix = dict(item)
        fix["source_threads"] = _normalize_source_threads(fix.get("source_threads"))
        fixes.append(fix)
    normalized["selected_fixes"] = fixes
    return normalized


def _reconstruct_task_from_thread(
    *,
    run: dict[str, Any],
    thread_messages: list[dict[str, Any]],
    source_created_at: dt.datetime,
    next_anchor_at: dt.datetime | None,
    user_name_by_id: dict[str, str] | None = None,
) -> dict[str, Any]:
    source_message_id = str(run.get("source_message_id") or "")
    source_message: dict[str, Any] | None = None
    for message in thread_messages:
        if message.get("metadata", {}).get("message_id") == source_message_id:
            source_message = message
            break

    ask_text = (
        source_message.get("text", "") if source_message else str(run.get("ask_text") or "")
    ).strip()
    source_user_name = (
        _message_user_display(source_message, user_name_by_id) if source_message else ""
    )
    source_user_id = _message_user_id(source_message or {})
    prior_messages = [
        message
        for message in thread_messages
        if isinstance(message.get("created_at"), dt.datetime)
        and message["created_at"] < source_created_at
    ]
    prior_context = prior_messages[-PRIOR_CONTEXT_WINDOW:]

    cutoff_at = source_created_at + dt.timedelta(hours=FOLLOWUP_CUTOFF_HOURS)
    if next_anchor_at is not None:
        cutoff_at = min(cutoff_at, next_anchor_at)

    followups = [
        message
        for message in thread_messages
        if isinstance(message.get("created_at"), dt.datetime)
        and source_created_at < message["created_at"] < cutoff_at
        and message.get("role") == "user"
    ][:FOLLOWUP_LIMIT]

    channel, thread_ts = _parse_thread_key(str(run.get("thread_key") or ""))
    task_id = f"task:{channel}:{thread_ts}:{source_message_id or run.get('run_id') or 'unknown'}"
    return {
        "task_id": task_id,
        "thread_key": str(run.get("thread_key") or ""),
        "channel": channel,
        "thread_ts": thread_ts,
        "source_message_id": source_message_id,
        "source_created_at": source_created_at.isoformat(),
        "source_user_id": source_user_id,
        "source_user_mention": _slack_user_mention(source_user_id),
        "source_user_name": source_user_name,
        "ask_text": ask_text,
        "prior_context": _serialize_messages(prior_context, user_name_by_id),
        "followups": _serialize_messages(followups, user_name_by_id),
        "workflow_run_id": str(run.get("run_id") or ""),
    }


async def _fetch_thread_messages(ctx: WorkflowContext, thread_key: str) -> list[dict[str, Any]]:
    rows = await ctx._pool.fetch(
        "SELECT id, role, parts, metadata, created_at "
        "FROM chat_messages WHERE thread_key = $1 ORDER BY created_at ASC",
        thread_key,
    )
    return [_normalize_message(dict(row)) for row in rows]


async def _fetch_live_thread_messages(
    ctx: WorkflowContext,
    *,
    thread_key: str,
) -> list[dict[str, Any]]:
    channel, thread_ts = _parse_thread_key(thread_key)

    async def _fetch() -> list[dict[str, Any]]:
        from api.app import get_tool_manager

        tm = get_tool_manager()
        raw = await tm.call_tool(
            "slack",
            "get_thread_replies",
            {"channel_id": channel, "thread_ts": thread_ts, "limit": 50},
        )
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return []
        return data if isinstance(data, list) else []

    step_name = f"live_thread_{channel}_{thread_ts.replace('.', '_')}"
    replies = await ctx.step(step_name, _fetch, step_kind="tool_call")
    messages: list[dict[str, Any]] = []
    for entry in replies:
        if not isinstance(entry, dict):
            continue
        created_at = _slack_ts_to_datetime(str(entry.get("ts") or entry.get("thread_ts") or ""))
        if created_at is None:
            continue
        role = "assistant"
        if not entry.get("bot_id") and entry.get("subtype") != "bot_message":
            role = "user"
        text = str(entry.get("text") or "").strip()
        messages.append(
            {
                "id": str(entry.get("ts") or ""),
                "role": role,
                "parts": [{"type": "text", "text": text}] if text else [],
                "metadata": {
                    "message_id": f"slack:{entry.get('ts') or ''}",
                    "user_id": str(entry.get("user") or "").strip(),
                },
                "created_at": created_at,
                "text": text,
                "part_types": ["text"] if text else [],
            }
        )
    return messages


async def _fetch_execution_details(
    ctx: WorkflowContext,
    *,
    run_id: str,
) -> dict[str, Any]:
    execution_id = await ctx._pool.fetchval(
        "SELECT execution_id FROM workflow_checkpoints "
        "WHERE run_id = $1 AND execution_id IS NOT NULL "
        "ORDER BY created_at DESC LIMIT 1",
        run_id,
    )
    if not execution_id:
        return {}

    execution_row = await ctx._pool.fetchrow(
        "SELECT execution_id, status, terminal_reason, result_text, error_text "
        "FROM agent_execution_requests WHERE execution_id = $1",
        execution_id,
    )
    summary_row = await ctx._pool.fetchrow(
        "SELECT event_json FROM agent_execution_events "
        "WHERE execution_id = $1 AND event_kind = 'execution_summary' "
        "ORDER BY event_id DESC LIMIT 1",
        execution_id,
    )

    payload: dict[str, Any] = {"execution_id": str(execution_id)}
    if execution_row:
        execution = dict(execution_row)
        payload.update(
            {
                "status": str(execution.get("status") or ""),
                "terminal_reason": str(execution.get("terminal_reason") or ""),
                "final_delivery_text": str(
                    execution.get("result_text") or execution.get("error_text") or ""
                ).strip(),
            }
        )

    if summary_row:
        summary = decode_jsonb(dict(summary_row).get("event_json"), {})
        if isinstance(summary, dict):
            payload.update(
                {
                    "duration_s": summary.get("duration_s"),
                    "ttft_ms": summary.get("ttft_ms"),
                    "tool_calls_by_name": summary.get("tool_calls_by_name", {}),
                    "tool_errors_by_name": summary.get("tool_errors_by_name", {}),
                    "tool_error_categories": summary.get("tool_error_categories", {}),
                    "tool_retry_count": summary.get("tool_retry_count", 0),
                    "subagent_events": summary.get("subagent_events", 0),
                    "subagent_failures": summary.get("subagent_failures", 0),
                    "command_error_events": summary.get("command_error_events", 0),
                    "file_change_events": summary.get("file_change_events", 0),
                    "total_tokens": summary.get("total_tokens", 0),
                    "cost_usd": summary.get("cost_usd"),
                    "models": summary.get("models", []),
                    "persona_id": summary.get("persona_id", ""),
                    "prompt_ref": summary.get("prompt_ref", ""),
                    "execution_sequence": summary.get("execution_sequence", 0),
                    "assistant_text_chars": summary.get("assistant_text_chars", 0),
                    "reasoning_events": summary.get("reasoning_events", 0),
                }
            )

    return payload


async def _aggregate_execution_details(
    ctx: WorkflowContext,
    *,
    run_ids: list[str],
) -> dict[str, Any]:
    details = [await _fetch_execution_details(ctx, run_id=run_id) for run_id in run_ids]
    present = [detail for detail in details if detail]
    latest = present[-1] if present else {}
    return {
        "workflow_run_ids": list(run_ids),
        "execution_ids": [
            str(detail.get("execution_id"))
            for detail in present
            if str(detail.get("execution_id") or "").strip()
        ],
        "status": str(latest.get("status") or ""),
        "terminal_reason": str(latest.get("terminal_reason") or ""),
        "final_delivery_text": str(latest.get("final_delivery_text") or ""),
        "tool_calls_by_name": _sum_mapping_values(present, "tool_calls_by_name"),
        "tool_errors_by_name": _sum_mapping_values(present, "tool_errors_by_name"),
        "tool_error_categories": _sum_mapping_values(present, "tool_error_categories"),
        "tool_retry_count": sum(int(detail.get("tool_retry_count") or 0) for detail in present),
        "subagent_events": sum(int(detail.get("subagent_events") or 0) for detail in present),
        "subagent_failures": sum(int(detail.get("subagent_failures") or 0) for detail in present),
        "command_error_events": sum(
            int(detail.get("command_error_events") or 0) for detail in present
        ),
        "file_change_events": sum(int(detail.get("file_change_events") or 0) for detail in present),
        "duration_s": latest.get("duration_s"),
        "ttft_ms": latest.get("ttft_ms"),
        "total_tokens": sum(int(detail.get("total_tokens") or 0) for detail in present),
        "cost_usd": sum(float(detail.get("cost_usd") or 0.0) for detail in present),
        "models": sorted(
            {m for detail in present for m in (detail.get("models") or []) if isinstance(m, str)}
        ),
        "persona_id": str(latest.get("persona_id") or ""),
        "prompt_ref": str(latest.get("prompt_ref") or ""),
        "execution_sequence": max(
            (int(detail.get("execution_sequence") or 0) for detail in present), default=0
        ),
        "assistant_text_chars": sum(
            int(detail.get("assistant_text_chars") or 0) for detail in present
        ),
        "reasoning_events": sum(int(detail.get("reasoning_events") or 0) for detail in present),
    }


async def _fetch_user_name_cache(ctx: WorkflowContext) -> dict[str, str]:
    """Fetch the workspace-wide `user_id → display_name` map via the Slack tool.

    The slackbot currently only persists ``user_id`` in message metadata,
    so every scorecard narrative would otherwise have to say "a user".
    We call ``slack.get_user_cache`` once per run, cache-friendly and
    bounded (~1000 users max), and use the result to hydrate names on
    evidence packs. On any failure we return an empty dict and the
    workflow degrades gracefully back to the previous "a user" behavior.
    """

    async def _fetch() -> dict[str, str]:
        from api.app import get_tool_manager

        tm = get_tool_manager()
        try:
            raw = await tm.call_tool("slack", "get_user_cache", {})
        except Exception as exc:
            ctx.log("self_improve_user_cache_failed", error=str(exc))
            return {}
        try:
            data = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items() if v}

    cache = await ctx.step("fetch_user_name_cache", _fetch, step_kind="tool_call")
    if not isinstance(cache, dict):
        return {}
    ctx.log("self_improve_user_cache_loaded", size=len(cache))
    return cache


def _review_window_since(review_window_hours: int) -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=review_window_hours)


async def _load_review_window_counts(
    ctx: WorkflowContext,
    *,
    review_window_hours: int,
) -> dict[str, int]:
    since = _review_window_since(review_window_hours)
    row = await ctx._pool.fetchrow(
        "SELECT COUNT(*) AS eligible_run_count, "
        "       COUNT(DISTINCT thread_key) AS eligible_thread_count "
        "FROM workflow_runs "
        "WHERE workflow_name = 'slack_thread_turn' "
        "  AND created_at >= $1 "
        "  AND status IN ('completed', 'failed', 'cancelled')",
        since,
    )
    data = dict(row or {})
    return {
        "eligible_run_count": int(data.get("eligible_run_count") or 0),
        "eligible_thread_count": int(data.get("eligible_thread_count") or 0),
    }


async def _collect_evidence_packs(
    ctx: WorkflowContext,
    *,
    review_window_hours: int,
    candidate_limit: int,
    user_name_by_id: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    since = _review_window_since(review_window_hours)
    rows = await ctx._pool.fetch(
        "SELECT run_id, thread_key, input_json, created_at "
        "FROM workflow_runs "
        "WHERE workflow_name = 'slack_thread_turn' "
        "  AND created_at >= $1 "
        "  AND status IN ('completed', 'failed', 'cancelled') "
        "ORDER BY created_at DESC",
        since,
    )

    candidate_runs: list[dict[str, Any]] = []
    for row in rows:
        row_data = dict(row)
        input_json = decode_jsonb(row_data.get("input_json"), {})
        parts = input_json.get("parts") if isinstance(input_json, dict) else []
        ask_text = _message_text(parts if isinstance(parts, list) else [])
        thread_key = str(row_data.get("thread_key") or "")
        if not thread_key:
            continue
        candidate_runs.append(
            {
                "run_id": str(row_data.get("run_id") or ""),
                "thread_key": thread_key,
                "created_at": row_data.get("created_at"),
                "source_message_id": input_json.get("message_id") if isinstance(input_json, dict) else "",
                "ask_text": ask_text,
            }
        )

    runs_by_thread: dict[str, list[dict[str, Any]]] = {}
    for run in candidate_runs:
        runs_by_thread.setdefault(run["thread_key"], []).append(run)

    tasks: list[dict[str, Any]] = []
    for thread_key, thread_runs in runs_by_thread.items():
        messages = await _fetch_thread_messages(ctx, thread_key)
        clusters: dict[str, dict[str, Any]] = {}
        for run in thread_runs:
            source_time = run.get("created_at")
            if not isinstance(source_time, dt.datetime):
                continue
            source_message_id = str(run.get("source_message_id") or "")
            for message in messages:
                if message.get("metadata", {}).get("message_id") == source_message_id:
                    source_time = message.get("created_at")
                    break
            if not isinstance(source_time, dt.datetime):
                continue
            cluster_key = source_message_id or str(run.get("run_id") or "")
            cluster = clusters.setdefault(
                cluster_key,
                {
                    "thread_key": thread_key,
                    "source_message_id": source_message_id,
                    "ask_text": str(run.get("ask_text") or "").strip(),
                    "run_ids": [],
                    "source_created_at": source_time,
                },
            )
            cluster["run_ids"].append(str(run.get("run_id") or ""))
            if not cluster.get("ask_text") and str(run.get("ask_text") or "").strip():
                cluster["ask_text"] = str(run.get("ask_text") or "").strip()
            if source_time < cluster["source_created_at"]:
                cluster["source_created_at"] = source_time

        task_anchors = sorted(
            list(clusters.values()),
            key=lambda item: item["source_created_at"],
        )
        for index, anchor in enumerate(task_anchors):
            source_time = anchor["source_created_at"]
            next_anchor_at = (
                task_anchors[index + 1]["source_created_at"]
                if index + 1 < len(task_anchors)
                else None
            )
            task = _reconstruct_task_from_thread(
                run={
                    "run_id": str(anchor["run_ids"][0] or ""),
                    "thread_key": thread_key,
                    "source_message_id": anchor["source_message_id"],
                    "ask_text": anchor["ask_text"],
                },
                thread_messages=messages,
                source_created_at=source_time,
                next_anchor_at=next_anchor_at,
                user_name_by_id=user_name_by_id,
            )
            if _looks_insufficient(task):
                live_messages = await _fetch_live_thread_messages(ctx, thread_key=thread_key)
                if live_messages:
                    task = _reconstruct_task_from_thread(
                        run={
                            "run_id": str(anchor["run_ids"][0] or ""),
                            "thread_key": thread_key,
                            "source_message_id": anchor["source_message_id"],
                            "ask_text": anchor["ask_text"],
                        },
                        thread_messages=live_messages,
                        source_created_at=source_time,
                        next_anchor_at=next_anchor_at,
                        user_name_by_id=user_name_by_id,
                    )

            execution = await _aggregate_execution_details(ctx, run_ids=list(anchor["run_ids"]))
            followup_summary = _summarize_followups(task["followups"])
            evidence = {
                "task_id": task["task_id"],
                "thread_key": task["thread_key"],
                "channel": task["channel"],
                "thread_ts": task["thread_ts"],
                "source_message_id": task["source_message_id"],
                "source_created_at": task["source_created_at"],
                "source_user_id": task.get("source_user_id", ""),
                "source_user_mention": task.get("source_user_mention", ""),
                "source_user_name": task.get("source_user_name", ""),
                "ask_text": task["ask_text"],
                "prior_context": task["prior_context"],
                "followups": task["followups"],
                "workflow_run_ids": execution.get("workflow_run_ids", [task["workflow_run_id"]]),
                "execution_ids": execution.get("execution_ids", []),
                "final_delivery_text": str(
                    execution.get("final_delivery_text") or ""
                )[:MAX_DELIVERY_TEXT_CHARS],
                "status": execution.get("status", ""),
                "terminal_reason": execution.get("terminal_reason", ""),
                "tool_calls_by_name": execution.get("tool_calls_by_name", {}),
                "tool_errors_by_name": execution.get("tool_errors_by_name", {}),
                "tool_error_categories": execution.get("tool_error_categories", {}),
                "tool_retry_count": execution.get("tool_retry_count", 0),
                "subagent_events": execution.get("subagent_events", 0),
                "subagent_failures": execution.get("subagent_failures", 0),
                "command_error_events": execution.get("command_error_events", 0),
                "file_change_events": execution.get("file_change_events", 0),
                "duration_s": execution.get("duration_s"),
                "ttft_ms": execution.get("ttft_ms"),
                "total_tokens": execution.get("total_tokens", 0),
                "cost_usd": execution.get("cost_usd"),
                "models": execution.get("models", []),
                "persona_id": execution.get("persona_id", ""),
                "prompt_ref": execution.get("prompt_ref", ""),
                "execution_sequence": execution.get("execution_sequence", 0),
                "assistant_text_chars": execution.get("assistant_text_chars", 0),
                "reasoning_events": execution.get("reasoning_events", 0),
                "user_followup_summary": followup_summary,
            }
            tasks.append(evidence)
            ctx.log(
                "self_improve_task_reconstructed",
                task_id=evidence["task_id"],
                thread_key=evidence["thread_key"],
            )

    ctx.log(
        "self_improve_tasks_reconstructed",
        task_count=len(tasks),
    )
    return tasks


async def _run_batch_review_pass(
    ctx: WorkflowContext,
    *,
    evidence_packs: list[dict[str, Any]],
    max_selected_fixes: int,
    recent_fix_titles: list[str] | None = None,
    batch_tag: str = "main",
) -> dict[str, Any]:
    if not evidence_packs:
        return _empty_review(0)

    recent_titles_block = ""
    if recent_fix_titles:
        recent_titles_block = textwrap.dedent(
            f"""
            Recently attempted fix titles (skip or justify re-attempting):
            ```json
            {json.dumps(recent_fix_titles, indent=2)}
            ```
            """
        ).strip()

    prompt = textwrap.dedent(
        f"""
        Load the `gap-analysis` skill first. Then read `references/rubric.md`.

        Review this batch of reconstructed Slack-thread user tasks.
        For each task, follow the evaluation method exactly:
        1. Restate the user's task in one sentence.
        2. Quote the key evidence from the ask, delivery, or follow-ups.
        3. Answer the binary sub-questions for each of the seven dimensions.
        4. Write a one-sentence reasoning trace per dimension.
        5. Assign the numeric score (0-4) per dimension.
        6. Compute the composite score.
        7. Classify as above-bar or below-bar.

        CRITICAL — use exactly these seven dimension keys in `scores` and `reasoning`:
        `completion`, `correctness`, `research_quality`, `verification_quality`,
        `tool_calling_quality`, `subagent_usage_quality`, `communication_quality`.
        Do NOT use alternate names like task_understanding, efficiency, user_satisfaction,
        instruction_following, or any other synonym. Use exactly the keys above.

        Interpret follow-up messages semantically. Read the actual text of each
        follow-up and determine whether it indicates satisfaction, correction,
        a new request, or frustration. Do not rely on keyword matching.

        Some tasks may be genuinely good. If a task was completed correctly with no
        negative follow-up signals, grade it above-bar. Do not manufacture problems
        where none exist. For conversational brainstorming or ideation tasks where
        verification is not applicable, score verification_quality as 4.

        Critical scoring rule: if a task failed, was cancelled, timed out, or
        never delivered a usable result, do NOT use "not applicable -> 4" for
        verification_quality or subagent_usage_quality. Score the failure that
        actually happened.

        Before finalizing the batch, compare the strongest below-bar task to the
        weakest above-bar task. If the ordering feels wrong, revise the scores
        before you return the JSON.

        After grading all tasks, cluster failures and select fixes.
        Keep clustering simple: dominant failure mode + likely fix surface.
        Prioritize user-value failures before style or polish.
        Respect the maximum selected-fix count: {max_selected_fixes}.

        Return JSON only. Use EXACTLY these top-level keys:
        `tasks_reviewed`, `below_bar_count`, `below_bar_rate`, `task_reviews`,
        `top_failure_modes`, `selected_fixes`.

        Each `selected_fixes` entry MUST include:
        `title`, `fix_type`, `target_surface`, `what_to_change`,
        `dominant_failure_mode`, `priority`, `why_now`, `evidence_quotes`,
        `source_threads`, `representative_tasks`, `slack_narrative`.
        The `target_surface` must name a real file in the Centaur codebase.
        Vague recommendations are not acceptable.

        `slack_narrative` is a 2-4 sentence human note used internally for
        archival. Always refer to the subject as "Centaur" (never "the bot"
        or "the agent"). Use `source_user_name` to refer to the user
        (plain first name only — never `source_user_mention` or raw Slack
        IDs). Name who surfaced the issue and describe what they were
        trying to do. This field is stripped before any PR is written.

        Also emit an OPTIONAL `credit_line` — one short warm sentence
        (≤120 chars) that credits the user for the specific thing they
        did well, using plain `source_user_name` (never a mention, never
        raw IDs). Good shapes: "good catch from Katie", "made possible
        because Matt asked for the gsuite tool directly". Only emit
        `credit_line` when the evidence credibly supports a specific good
        ask or observation. If it doesn't, leave `credit_line` as the
        empty string and the scorecard will fall back to a plain name.
        Never invent a credit line — evidence first, warmth second.

        Prefer structural fix types (workflow_fix, bug_fix, tool_improvement,
        new_skill, new_persona) when the root cause is structural. Reach for
        `prompt_tweak` only when the root cause is a genuine instructional
        gap — if perfect prompt compliance would not prevent the failure, a
        code-level fix is the right answer even if the diff is bigger.

        Progress reporting: Generating the final review JSON can be long.
        Between evaluating each task (before you start writing the final JSON
        answer), emit a tiny `shell_command` call like `echo "reviewing N/M"`.
        These shell calls reset the silence watchdog to 30 minutes and are
        ignored by the parser. Do this after every 1-2 tasks you evaluate.
        Then write the final JSON answer as your last message.

        {recent_titles_block}

        Evidence pack batch:
        ```json
        {json.dumps({"max_selected_fixes": max_selected_fixes, "tasks": evidence_packs}, indent=2)}
        ```
        """
    ).strip()

    review_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:gap-analysis:{batch_tag}",
        message_id=f"wf:{ctx.run_id}:batch_review:{batch_tag}",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "batch_review",
            "batch_tag": batch_tag,
        },
    )

    async def _parse_review() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(review_turn.get("result_text") or ""),
            stage="batch_review",
            preferred_keys=REVIEW_PREFERRED_KEYS,
            required_keys=REVIEW_REQUIRED_KEYS,
        )

    async def _repair_review() -> dict[str, Any]:
        malformed = str(review_turn.get("result_text") or "").strip()
        repair_prompt = textwrap.dedent(
            f"""
            Your previous `batch_review` response was malformed for the
            self-improvement workflow.

            Return JSON only with exactly these top-level keys:
            `tasks_reviewed`, `below_bar_count`, `below_bar_rate`,
            `task_reviews`, `top_failure_modes`, `selected_fixes`.

            Do not return any prose, explanation, or markdown fences.
            Re-use the evidence already in this thread. If you are missing a
            field, infer it conservatively from the existing evidence instead
            of omitting the key.

            Previous malformed response:
            ```text
            {malformed[:2000]}
            ```
            """
        ).strip()
        repaired_turn = await ctx.agent_turn(
            repair_prompt,
            thread_key=f"workflow:{ctx.run_id}:gap-analysis:{batch_tag}",
            message_id=f"wf:{ctx.run_id}:batch_review:repair:{batch_tag}",
            delivery=Delivery.dev(),
            prompt_selector="eng",
            metadata={
                "source": WORKFLOW_NAME,
                "mode": "parent",
                "stage": "batch_review_repair",
                "batch_tag": batch_tag,
            },
        )
        return _extract_required_json_payload(
            str(repaired_turn.get("result_text") or ""),
            stage="batch_review_repair",
            preferred_keys=REVIEW_PREFERRED_KEYS,
            required_keys=REVIEW_REQUIRED_KEYS,
        )

    try:
        review = await ctx.step("batch_review", _parse_review, step_kind="review")
    except RuntimeError as exc:
        ctx.log("self_improve_batch_review_repair_requested", error=str(exc))
        review = await ctx.step("batch_review_repair", _repair_review, step_kind="review")
    return _normalize_review(review, tasks_reviewed=len(evidence_packs))


async def _run_learning_synthesis_pass(
    ctx: WorkflowContext,
    *,
    evidence_packs: list[dict[str, Any]],
    max_selected_builds: int,
) -> dict[str, Any]:
    if not evidence_packs:
        return {"sessions_analyzed": 0, "opportunities_found": 0, "opportunities": [], "selected_builds": []}

    summaries = [_compact_task_summary(task) for task in evidence_packs]
    prompt = textwrap.dedent(
        f"""
        Load the `learning-synthesis` skill first.

        Analyze this batch of recent Slack-thread user sessions.
        Look for opportunities to improve the system — not quality bugs (those are
        handled separately by gap-analysis), but learnings:
        - Recurring demand patterns that should become new skills
        - Domains or stances that should become new personas
        - Knowledge the bot had to be taught that should be baked in
        - Tool capabilities users need but don't have
        - Manual workflows that should be automated
        - System prompt gaps that cause recurring confusion

        Focus on patterns across 2+ sessions, not one-off requests.
        Every opportunity must name a specific target_surface (file path) and a
        concrete implementation_sketch.
        Select all materially justified opportunities for autonomous implementation.

        Each `selected_builds` entry MUST include `slack_narrative` — 2-4
        sentences of plain-English prose. Always refer to the subject as
        "Centaur" (never "the bot" or "the agent"). Name the users who
        surfaced the pattern using plain `source_user_name` (never
        `source_user_mention`, never raw Slack IDs), describe what they
        were trying to do, and explain why this opportunity is worth
        building now. This narrative is stripped before the implementing
        agent sees the fix packet. Stay grounded in provided evidence —
        do not invent situations.

        Also emit an OPTIONAL `credit_line` on each selected_build — one
        short warm sentence (≤120 chars) crediting the user for the
        specific good ask or observation that surfaced this opportunity,
        using plain `source_user_name` only (never a mention, never raw
        IDs). Good shapes: "good ask from Arjun", "made possible because
        Katie kept asking for an editorial voice directly". Only emit
        `credit_line` when the evidence credibly supports a specific
        action; otherwise leave it as the empty string and the scorecard
        will fall back to a plain name.

        Return JSON only matching the output contract in the skill.

        Compact task summary batch:
        ```json
        {json.dumps({"max_selected_builds": max_selected_builds, "tasks": summaries}, indent=2)}
        ```
        """
    ).strip()

    synthesis_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:learning-synthesis",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "learning_synthesis",
        },
    )

    async def _parse_synthesis() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(synthesis_turn.get("result_text") or ""),
            stage="learning_synthesis",
            preferred_keys=SYNTHESIS_PREFERRED_KEYS,
            required_keys=SYNTHESIS_REQUIRED_KEYS,
        )

    synthesis = await ctx.step("learning_synthesis", _parse_synthesis, step_kind="review")
    if not isinstance(synthesis, dict):
        synthesis = {}
    synthesis.setdefault("sessions_analyzed", len(evidence_packs))
    synthesis.setdefault("opportunities_found", len(list(synthesis.get("opportunities") or [])))
    synthesis.setdefault("opportunities", [])
    synthesis.setdefault("selected_builds", [])
    for build in list(synthesis.get("selected_builds") or []):
        if isinstance(build, dict):
            build["source_threads"] = _normalize_source_threads(
                [{"thread_key": tk} for tk in list(build.get("evidence_threads") or [])]
            )
    return synthesis


def _mean_composite(review: dict[str, Any]) -> float:
    task_reviews = list(review.get("task_reviews") or [])
    if not task_reviews:
        return 0.0
    scores = []
    for task in task_reviews:
        if not isinstance(task, dict):
            continue
        composite = task.get("composite_score")
        if composite is not None:
            try:
                scores.append(float(composite))
            except (TypeError, ValueError):
                continue
    return round(sum(scores) / len(scores), 1) if scores else 0.0


def _slack_pr_link(pr_number: int | str, pr_url: str) -> str:
    """Render a PR link in Slack's native mrkdwn syntax.

    Slack does not render GitHub-style `[text](url)` markdown links in
    regular messages — they come through as literal text. Slack's own
    link format is `<url|text>`.
    """
    number = str(pr_number).strip()
    url = str(pr_url).strip()
    if not url:
        return f"#{number}" if number else ""
    if not number:
        return f"<{url}>"
    return f"<{url}|#{number}>"


def _slack_thread_archive_url(channel: str, thread_ts: str) -> str:
    """Build the `https://slack.com/archives/<channel>/p<compact_ts>` URL.

    Slack archive URLs use a compact timestamp format where the `.` in
    `1776374169.372999` becomes `p1776374169372999`. Returns empty string
    when we lack the data needed to build a usable link.
    """
    channel = str(channel or "").strip()
    ts = str(thread_ts or "").strip()
    if not channel or not ts:
        return ""
    compact = ts.replace(".", "")
    return f"https://slack.com/archives/{channel}/p{compact}"


def _render_source_thread_links(
    source_threads: list[dict[str, Any]] | None,
    *,
    link_text: str = "thread",
) -> str:
    """Render one or more source threads as Slack `<url|thread>` links.

    Returns an empty string when there are no usable entries so the caller
    can skip the line entirely. Multiple threads render comma-separated
    so the reader can jump to whichever session looks most relevant.
    """
    if not source_threads:
        return ""
    links: list[str] = []
    for entry in source_threads:
        if not isinstance(entry, dict):
            continue
        url = _slack_thread_archive_url(
            str(entry.get("channel") or ""),
            str(entry.get("thread_ts") or ""),
        )
        if url:
            links.append(f"<{url}|{link_text}>")
    return ", ".join(links)


def _clip(text: str, max_chars: int = 160) -> str:
    stripped = str(text or "").strip()
    if len(stripped) <= max_chars:
        return stripped
    return stripped[: max_chars - 1].rstrip() + "\u2026"


_MENTION_PATTERN = re.compile(r"<@U[A-Z0-9]+>")
_INLINE_WHITESPACE_COLLAPSE = re.compile(r"[ \t]{2,}")


def _strip_mentions(text: str) -> str:
    """Remove any `<@U...>` Slack mention patterns from *text*.

    Safety net: the scorecard must never leak raw Slack user IDs into a
    public channel. The LLM prompts ask for plain first names, but this
    helper catches any stray `<@UXXXX>` that slips through. Also
    collapses the double-space residue a mid-sentence mention leaves
    behind so the body doesn't look mechanical.

    Intended for body-level text only — do NOT apply this to a
    multi-line scorecard, because it will squash the two-space indent
    on sub-bullets into one space. Use ``_strip_mentions_multiline`` at
    the whole-message boundary instead.
    """
    cleaned = _MENTION_PATTERN.sub("", str(text or ""))
    cleaned = _INLINE_WHITESPACE_COLLAPSE.sub(" ", cleaned)
    return cleaned.strip()


def _strip_mentions_multiline(text: str) -> str:
    """Whole-message mention strip that preserves bullet indentation.

    Only removes `<@U...>` matches; leaves all whitespace alone so sub-
    bullets keep their two-space indent and blank separator lines stay
    intact. Strips leading/trailing whitespace on the overall block.
    """
    return _MENTION_PATTERN.sub("", str(text or "")).strip()


def _fallback_body(item: dict[str, Any], field_names: tuple[str, ...]) -> str:
    """Last-resort body when the polish LLM is unavailable.

    Takes the first non-empty structured field, strips any stray Slack
    mentions, and clips at a generous limit so Slack never truncates the
    post mid-word. We accept an awkwardly long sentence here because
    polish-failed fallback is better than a failed post; the primary
    path is always the LLM polish pass.
    """
    for name in field_names:
        raw = str(item.get(name) or "").strip()
        if not raw:
            continue
        return _clip(_strip_mentions(raw), max_chars=240)
    return ""


# Ten variants — seed-picked once per run so the post feels fresh without
# going random. The first five read naturally when a capability actually
# shipped; the last five read naturally on quiet nights. Never reference
# "the bot" or "the agent" — the subject is always Centaur.
_SCORECARD_FLAIR_SHIPPED = (
    "A new skill landed; a couple of fixes are cooling off in review.",
    "Turns out everyone here writes memos on Fridays. Centaur noticed and shipped an editorial voice for it.",
    "Portfolio reviews are apparently a daily ritual around here — Centaur learned the structured view you've been reinventing.",
    "The team keeps asking for cleaner Sheets writes; Centaur is finally in on the joke.",
    "A fresh capability you can poke at tomorrow and a couple of fixes waiting on human eyes.",
)

_SCORECARD_FLAIR_QUIET = (
    "Mostly calibration tonight — quiet wins and a few sharp corners.",
    "Everything is still gathering opinions in review.",
    "Centaur picked up a new trick tonight and left a couple of rough edges for us to look at.",
    "Small, useful night. A few fixes waiting on review.",
    "Light pass today. Mostly Centaur learning not to debug by redesign.",
)


def _scorecard_flair(
    review: dict[str, Any],
    synthesis: dict[str, Any],
    *,
    shipped_count: int,
) -> str:
    """Pick one flair line deterministically from the pool.

    The seed mixes in counts from the run so consecutive nights with
    different shapes land on different lines. `shipped_count` selects
    between the "something shipped" pool and the "quiet night" pool so
    the opening line never fights the body of the post.
    """
    seed = (
        int(review.get("tasks_reviewed") or 0)
        + int(review.get("below_bar_count") or 0)
        + len(list(review.get("selected_fixes") or []))
        + len(list(synthesis.get("selected_builds") or []))
        + int(shipped_count)
    )
    pool = _SCORECARD_FLAIR_SHIPPED if shipped_count > 0 else _SCORECARD_FLAIR_QUIET
    return pool[seed % len(pool)]


_FAILURE_MODE_EXPLANATIONS = {
    "verification_miss": "Centaur skipped or under-verified work before handoff.",
    "intent_miss": "Centaur answered a different problem than the one the user actually asked.",
    "debugging_intent_miss": "Centaur treated a debugging request like ideation instead of investigating the live system.",
    "intent_miss_investigation_replaced_by_ideation": (
        "Centaur proposed redesigns instead of inspecting the broken workflow first."
    ),
    "research_miss": "Centaur missed important evidence that was available in the repo, tools, or data.",
    "tool_misuse": "Centaur used the wrong tool path or missed the right tool entirely.",
    "reliability_issue": "Centaur failed before delivering a usable answer.",
    "reliability_timeout_before_progress": (
        "Active executions timed out before Centaur recognized startup progress."
    ),
    "missing_sheet_tab_capability": (
        "Centaur could not create the Google Sheet structure the user asked for."
    ),
}


def _humanize_failure_mode(entry: dict[str, Any]) -> str:
    failure_mode = str(entry.get("failure_mode") or "").strip()
    count = int(entry.get("count") or 0)
    if failure_mode in _FAILURE_MODE_EXPLANATIONS:
        label = _FAILURE_MODE_EXPLANATIONS[failure_mode]
    else:
        label = (
            failure_mode.replace("_", " ").strip().capitalize()
            if failure_mode
            else "Unspecified failure mode."
        )
        if label and not label.endswith("."):
            label += "."
    if count:
        task_word = "task" if count == 1 else "tasks"
        return f"{label} ({count} {task_word})"
    return label


def _build_polish_payload(
    review: dict[str, Any],
    synthesis: dict[str, Any],
    child_results: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    """Collect the per-item inputs the polish LLM needs to write bodies.

    Each item is trimmed to just the framing fields the LLM should
    consider so we keep the prompt small and focused. Item IDs are
    stable and index-based so the caller can map polished bodies back
    onto the rendered scorecard bullets deterministically.
    """

    def _trim(item: dict[str, Any], field_names: tuple[str, ...]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name in field_names:
            value = item.get(name)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, str):
                out[name] = _clip(_strip_mentions(value), max_chars=600)
            else:
                out[name] = value
        return out

    gap_fixes = [
        _trim(
            fix,
            (
                "title",
                "fix_type",
                "dominant_failure_mode",
                "why_now",
                "what_to_change",
                "what_should_exist",
            ),
        )
        | {"id": f"gap-{index}"}
        for index, fix in enumerate(
            [item for item in list(review.get("selected_fixes") or [])[:5] if isinstance(item, dict)]
        )
    ]
    builds = [
        _trim(
            build,
            (
                "title",
                "opportunity_type",
                "what_should_exist",
                "user_value",
                "implementation_sketch",
                "evidence_summary",
            ),
        )
        | {"id": f"growth-{index}"}
        for index, build in enumerate(
            [item for item in list(synthesis.get("selected_builds") or [])[:3] if isinstance(item, dict)]
        )
    ]

    shipped: list[dict[str, Any]] = []
    in_review: list[dict[str, Any]] = []
    for entry in child_results:
        if not isinstance(entry, dict):
            continue
        if entry.get("error") or not entry.get("pr_number") or not entry.get("pr_url"):
            continue
        trimmed = _trim(
            entry,
            (
                "title",
                "fix_type",
                "dominant_failure_mode",
                "why_now",
                "what_to_change",
                "what_should_exist",
                "user_value",
            ),
        )
        if str(entry.get("auto_merge_status") or "") == "merged":
            trimmed["id"] = f"shipped-{len(shipped)}"
            shipped.append(trimmed)
        else:
            trimmed["id"] = f"in_review-{len(in_review)}"
            in_review.append(trimmed)

    return {
        "gap": gap_fixes,
        "growth": builds,
        "shipped": shipped,
        "in_review": in_review,
    }


def _build_flair_digest(
    all_tasks: list[dict[str, Any]],
    *,
    limit: int = 24,
) -> list[dict[str, Any]]:
    """Compact per-thread digest that feeds the flair line's creative hook.

    Returns up to *limit* records of ``{user, ask, outcome}`` drawn
    from tonight's reconstructed tasks, one per unique thread so a
    chatty single thread can't dominate the signal. This gives the
    polish LLM enough raw material to anchor the opening line in a
    real topic or team pattern from tonight — ``portfolio reviews``,
    ``gsuite sheets writes``, ``memo drafting`` — rather than a
    generic "quiet night" template. User names are plain first names
    only; the polish prompt still rejects any raw `<@U...>` mention.
    """
    seen_thread_keys: set[str] = set()
    digest: list[dict[str, Any]] = []
    for task in all_tasks:
        if not isinstance(task, dict):
            continue
        ask = str(task.get("ask_text") or "").strip()
        if not ask:
            continue
        thread_key = str(task.get("thread_key") or "").strip()
        if thread_key and thread_key in seen_thread_keys:
            continue
        if thread_key:
            seen_thread_keys.add(thread_key)
        digest.append(
            {
                "user": str(task.get("source_user_name") or "").strip(),
                "ask": _clip(_strip_mentions(ask), max_chars=160),
                "outcome": str(task.get("status") or "unknown").strip()
                or "unknown",
            }
        )
        if len(digest) >= limit:
            break
    return digest


async def _polish_scorecard_bullets(
    ctx: WorkflowContext,
    *,
    review: dict[str, Any],
    synthesis: dict[str, Any],
    child_results: list[dict[str, Any]],
    flair_digest: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Polish bullet bodies and the flair line with one small LLM pass.

    Returns ``{"flair": str, "bodies": {item_id: str}}``. On any failure
    returns empty values so the scorecard falls back to the seeded
    flair pool and a strip+clip on the most informative raw field.
    Reliability first: the nightly post must go out even if this pass
    misbehaves, so every error is logged and swallowed.

    The pass is intentionally scoped small — it only writes the short
    per-bullet body text + one flair line. Section titles, attribution
    suffixes, and PR / thread links are still assembled deterministically
    by ``_build_scorecard_markdown`` so the tone spec can't drift into
    those structural pieces.

    ``flair_digest`` is a compact summary of tonight's reconstructed
    threads used ONLY to anchor the opening flair line in a real topic
    or team pattern that came up in Slack tonight. None or empty is
    fine — the LLM falls back to a generic (but still tight) opener.
    """
    payload = _build_polish_payload(review, synthesis, child_results)
    item_count = sum(len(v) for v in payload.values())
    digest = list(flair_digest or [])
    if item_count == 0 and not digest:
        return {"flair": "", "bodies": {}}

    prompt = textwrap.dedent(
        f"""
        You are polishing one pass of the nightly `#ai-agent` gap-analysis
        scorecard. The audience is everyone at the company who uses
        Centaur. Write like a teammate sharing a quick daily update, not
        like a release-notes generator.

        Voice:
        - The subject is ALWAYS **Centaur** — never "the bot", never
          "the agent".
        - Active voice, present tense, concrete verbs: picks up, handles,
          routes, lands, tightens.
        - Occasional dry wit, occasional self-deprecation at Centaur's
          past stumbles, or — very rarely, when it clearly lands — a
          light tease of a user. Never mean-spirited.
        - Humor (when it lands) targets Centaur's behavior. Subtle
          team-pattern jokes are OK when they're positive/growth-mindset
          ("you all kept asking for X — Centaur finally caught up"),
          never mean-spirited.
        - No corporate openers ("We're excited to…", "Proud to announce…").
        - No AI/PM jargon ("leverage", "orchestrated", "cross-functional").
        - No rule-of-three lists, no em-dash pile-ups, no hype for a
          one-line prompt tweak.
        - No emoji.

        Hard constraints on EVERY body:
        - ONE short sentence, ~120–180 characters. Never more than two
          sentences. Never longer than 220 characters.
        - Never include the user's name, `<@USER_ID>` mentions, PR links,
          or thread links — the renderer adds those separately.
        - Never use internal terms in the body ("gap analysis",
          "self-improvement loop", "below-bar"). Those are structural;
          the body is about what Centaur does for the team.
        - No sign-off, no "—", no leading bullet. Return the body text
          only.

        Shape by section:
        - `gap`: why this matters to fix — what Centaur gets wrong,
          framed concretely. Example shape: "Debugging asks keep
          turning into ideation sessions — the prompt should push
          Centaur to investigate first."
        - `growth`: what the new capability unlocks — one concrete user
          moment. Example shape: "Editorial persona for decision memos
          — memo asks surfaced across three different threads this
          week."
        - `shipped`: invite the reader to use it. For new_skill /
          new_persona, start with "now available when you …" or
          similar. For prompt_tweak, name the behavior change then end
          with "Live." Example shapes: "ask how positions are tracking
          vs the market and Centaur picks up this view automatically.
          Try it on your next portfolio check." / "Centaur used to
          answer debugging asks by proposing redesigns; now it actually
          goes and looks. Live."
        - `in_review`: one short sentence naming what changed. Example:
          "The silence watchdog kept mistaking startup for stalling;
          this fix teaches Centaur the difference."

        The `flair` line is the primary creativity surface — write it
        last, and make it specific to tonight. Anchor it in a real
        topic, user moment, or team pattern you can see in the
        `flair_digest` below. Good shapes:

        - Team pattern ("you all kept asking for X"):
          `Turns out everyone here writes memos on Fridays. Centaur
          noticed and shipped an editorial voice for it.`
        - Recurring topic (≥2 asks in different threads tonight):
          `Portfolio reviews are apparently a daily ritual around
          here — Centaur learned the structured view you've been
          reinventing.`
        - Self-deprecation tied to tonight's actual stumble:
          `Centaur tried to debug three Sheets exports by suggesting
          redesigns. Fixed the impulse; tab-writing is still catching up.`
        - Micro-moment (one person's specific ask, called out warmly):
          `Katie asked for a decision-memo voice this afternoon and
          the editorial persona landed by bedtime.`

        Flair rules:
        - ~120 characters. ONE sentence, never two.
        - Anchor in something actually present in `flair_digest` or
          the item lists — never invent a topic. If the digest is
          empty or there's nothing concrete to hook into, fall back
          to a Centaur-behavior self-deprecation line. Never generic
          temperament phrases ("an honest night", "a solid pass").
        - Plain first names only (never `<@UXXXX>`, never "a user").
          Use the `user` field from `flair_digest` verbatim. Omit the
          name entirely when you don't have one — do not invent one.
        - Paraphrase what users asked about; never quote asks word for
          word. The flair is inspired by the digest, not a transcript.
        - Warm when something shipped; dry when nothing did.
        - Humor is optional — tonight's digest may not offer a clean
          angle, and that's fine. A tight factual opener beats a
          forced joke.

        Return JSON ONLY with EXACTLY this shape (no prose, no fences,
        no explanation):
        {{
          "flair": "…",
          "bodies": {{
            "<item_id>": "<body>"
          }}
        }}

        Input items (write one body per id):
        ```json
        {json.dumps(payload, indent=2, ensure_ascii=False)}
        ```

        `flair_digest` (anchor the flair here; may be empty on a quiet
        night):
        ```json
        {json.dumps(digest, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()

    # Let SuspendWorkflow / CancelledWorkflow / NonRetryableError propagate
    # — they are workflow-engine control signals, not failures we can
    # swallow. Any genuine exception (network blip etc.) will ride the
    # normal step retry path; if all retries exhaust the workflow fails
    # loudly and we learn about it on the next deploy, which is the
    # right signal. What we DO protect against here is "execution
    # succeeded but returned garbage" — the parse step below handles
    # that by degrading to empty polish.
    polish_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:scorecard-polish",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "scorecard_polish",
        },
    )

    async def _parse() -> dict[str, Any]:
        raw = str(polish_turn.get("result_text") or "")
        try:
            parsed = extract_json_payload(raw, preferred_keys=("flair", "bodies"))
        except Exception:
            return {"flair": "", "bodies": {}}
        flair = _strip_mentions(str(parsed.get("flair") or "").strip())
        bodies_raw = parsed.get("bodies") or {}
        bodies: dict[str, str] = {}
        if isinstance(bodies_raw, dict):
            for key, value in bodies_raw.items():
                text = _strip_mentions(str(value or "").strip())
                if not text:
                    continue
                # Cap each body at a generous limit so a misbehaving
                # polish run can never explode the Slack message size.
                bodies[str(key)] = _clip(text, max_chars=220)
        return {"flair": _clip(flair, max_chars=180), "bodies": bodies}

    try:
        return await ctx.step("scorecard_polish_parse", _parse, step_kind="review")
    except (SuspendWorkflow, CancelledWorkflow, NonRetryableError):
        # Control-flow signals must propagate so the workflow engine
        # can suspend / cancel / fail cleanly.
        raise
    except Exception as exc:
        # Parse genuinely failed (malformed JSON, unexpected shape).
        # Degrade silently so the scorecard still posts.
        ctx.log("self_improve_polish_parse_failed", error=str(exc))
        return {"flair": "", "bodies": {}}


def _thread_suffix(source_threads: list[dict[str, Any]] | None) -> str:
    """Render `· <url|thread>` suffix when source threads are available.

    Returns empty string when nothing is usable so the caller can simply
    concatenate without branching.
    """
    links = _render_source_thread_links(source_threads, link_text="thread")
    return f" · {links}" if links else ""


def _attribution_suffix(
    item: dict[str, Any],
    thread_user_names: dict[str, str] | None,
) -> str:
    """Render `· {credit_line or "from {name}"}` suffix when available.

    Prefers the warm `credit_line` emitted by the review/synthesis pass
    ("good catch from Katie"); falls back to `· from {first_name}` when
    we have a user name via thread_user_names. Returns empty string
    when we have nothing — we never say "anonymous" or "a user".
    """
    credit = _strip_mentions(str(item.get("credit_line") or "").strip())
    if credit:
        return f" · {_clip(credit, max_chars=140)}"
    if not thread_user_names:
        return ""
    for thread in list(item.get("source_threads") or []):
        if not isinstance(thread, dict):
            continue
        key = str(thread.get("thread_key") or "").strip()
        if not key:
            continue
        name = str(thread_user_names.get(key) or "").strip()
        if name:
            return f" · from {name}"
    return ""


def _classify_child_entries(
    child_results: list[dict[str, Any]] | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split child results into `(shipped, in_review)` by auto_merge_status.

    Shipped entries are the ones the auto-merge gate squash-merged this
    run. Everything else with a PR number and no error lands in review.
    Child errors (no PR, explicit error field) are dropped entirely per
    the plan — the scorecard stays about outcomes, not misfires. ``None``
    or non-list inputs yield two empty lists so the scorecard renderer
    never crashes on a malformed upstream result.
    """
    shipped: list[dict[str, Any]] = []
    in_review: list[dict[str, Any]] = []
    if not isinstance(child_results, list):
        return shipped, in_review
    for entry in child_results:
        if not isinstance(entry, dict):
            continue
        if entry.get("error") or not entry.get("pr_number") or not entry.get("pr_url"):
            continue
        if str(entry.get("auto_merge_status") or "") == "merged":
            shipped.append(entry)
        else:
            in_review.append(entry)
    return shipped, in_review


def _build_scorecard_markdown(
    *,
    review: dict[str, Any] | None,
    synthesis: dict[str, Any] | None,
    child_results: list[dict[str, Any]] | None,
    coverage: dict[str, int] | None = None,
    thread_user_names: dict[str, str] | None = None,
    merged_prs_24h: int | None = None,
    polished_bodies: dict[str, str] | None = None,
    polished_flair: str | None = None,
) -> str:
    """Build the nightly #ai-agent gap-analysis scorecard.

    The layout is deliberately concise and coworker-voiced: a short
    italic summary line on top, one flair line, then a few tight
    sections. Per-bullet bodies are polished by a small LLM pass
    upstream (`_polish_scorecard_bullets`); this function only handles
    structure, attribution suffixes, and link formatting. When the
    polish pass doesn't produce a body for an item, we fall back to
    stripping mentions + a generous clip on the raw framing fields.

    Flat lines are joined by "\\n" (no textwrap.dedent) to avoid the
    indent-drift bug that produced the mangled 8-space-indented
    scorecard posts historically.

    All dict / list arguments accept ``None`` or malformed shapes and
    degrade to an empty section rather than crashing — the nightly
    post must go out even when an upstream step returned something
    unexpected.
    """
    review = review if isinstance(review, dict) else {}
    synthesis = synthesis if isinstance(synthesis, dict) else {}
    polished_bodies = polished_bodies if isinstance(polished_bodies, dict) else {}
    thread_user_names = thread_user_names if isinstance(thread_user_names, dict) else {}
    coverage = coverage if isinstance(coverage, dict) else {}

    reviewed = int(review.get("tasks_reviewed") or 0)
    below_bar = int(review.get("below_bar_count") or 0)
    reconstructed_threads = int(coverage.get("reconstructed_thread_count", 0) or 0)
    top_failure_modes = [
        entry
        for entry in list(review.get("top_failure_modes") or [])[:3]
        if isinstance(entry, dict)
    ]
    gap_fixes = [
        item
        for item in list(review.get("selected_fixes") or [])[:5]
        if isinstance(item, dict)
    ]
    selected_builds = [
        item
        for item in list(synthesis.get("selected_builds") or [])[:3]
        if isinstance(item, dict)
    ]
    opportunities = [
        item
        for item in list(synthesis.get("opportunities") or [])[:3]
        if isinstance(item, dict)
    ]
    growth_items = selected_builds or opportunities
    shipped, in_review = _classify_child_entries(child_results)

    summary_parts = [f"Reviewed {reviewed} tasks"]
    if reconstructed_threads:
        summary_parts[-1] += f" across {reconstructed_threads} threads"
    summary_parts[-1] += "."
    if below_bar:
        summary_parts.append(f"{below_bar} below the bar.")
    if merged_prs_24h is not None and merged_prs_24h >= 0:
        if merged_prs_24h == 1:
            summary_parts.append("1 self-improve PR merged in the last 24h.")
        elif merged_prs_24h > 1:
            summary_parts.append(
                f"{merged_prs_24h} self-improve PRs merged in the last 24h."
            )
    summary_line = f"_{' '.join(summary_parts)}_"

    flair_line = (
        _strip_mentions(str(polished_flair or "").strip())
        or _scorecard_flair(review, synthesis, shipped_count=len(shipped))
    )

    lines: list[str] = [
        "*Nightly gap analysis*",
        summary_line,
        "",
        flair_line,
    ]

    # Gap analysis section — top failure modes and selected fixes.
    if top_failure_modes or gap_fixes:
        lines.extend(["", "*Gap analysis*"])
    if top_failure_modes:
        lines.append("• Top failure modes")
        for entry in top_failure_modes:
            lines.append(f"  • {_humanize_failure_mode(entry)}")
    if gap_fixes:
        lines.append("• Selected fixes")
        for index, fix in enumerate(gap_fixes):
            title = str(fix.get("title") or "Untitled fix").strip()
            body = polished_bodies.get(f"gap-{index}") or _fallback_body(
                fix, ("why_now", "what_to_change", "slack_narrative")
            )
            attribution = _attribution_suffix(fix, thread_user_names)
            thread = _thread_suffix(fix.get("source_threads"))
            segment = f"{title} — {body}" if body else title
            lines.append(f"  • {segment}{attribution}{thread}")

    # Growth opportunities — capabilities the team would benefit from.
    if growth_items:
        lines.extend(["", "*Growth opportunities*"])
        for index, build in enumerate(growth_items):
            title = str(build.get("title") or "Untitled").strip()
            body = polished_bodies.get(f"growth-{index}") or _fallback_body(
                build,
                ("what_should_exist", "user_value", "implementation_sketch", "slack_narrative"),
            )
            attribution = _attribution_suffix(build, thread_user_names)
            thread = _thread_suffix(build.get("source_threads"))
            segment = f"{title} — {body}" if body else title
            lines.append(f"• {segment}{attribution}{thread}")

    # Shipped tonight — auto-merged PRs only.
    if shipped:
        lines.extend(["", "*Shipped tonight*"])
        for index, entry in enumerate(shipped):
            title = str(entry.get("title") or "Untitled").strip()
            body = polished_bodies.get(f"shipped-{index}") or _fallback_body(
                entry,
                (
                    "what_should_exist",
                    "user_value",
                    "why_now",
                    "what_to_change",
                    "slack_narrative",
                ),
            )
            pr_link = _slack_pr_link(
                entry.get("pr_number", ""), str(entry.get("pr_url") or "")
            )
            segment = f"{title} — {body}" if body else title
            tail = f" {pr_link}" if pr_link else ""
            lines.append(f"• {segment}{tail}")

    # In review — remaining PRs that didn't auto-merge.
    if in_review:
        lines.extend(["", "*In review*"])
        for index, entry in enumerate(in_review):
            title = str(entry.get("title") or "Untitled").strip()
            body = polished_bodies.get(f"in_review-{index}") or _fallback_body(
                entry,
                ("what_to_change", "why_now", "slack_narrative"),
            )
            pr_link = _slack_pr_link(
                entry.get("pr_number", ""), str(entry.get("pr_url") or "")
            )
            segment = f"{title} — {body}" if body else title
            tail = f" {pr_link}" if pr_link else ""
            lines.append(f"• {segment}{tail}")

    rendered = "\n".join(lines).strip()
    # Final safety net: even if everything upstream slips a mention
    # through, this regex-strip guarantees no raw `<@UXXXX>` lands in a
    # public channel. Uses the multiline variant so bullet indentation
    # and blank separator lines are preserved.
    return _strip_mentions_multiline(rendered) or rendered


SLACK_ONLY_FIX_FIELDS = ("slack_narrative", "credit_line")


def _strip_slack_only_fields(fix: dict[str, Any]) -> dict[str, Any]:
    """Return a copy of *fix* with Slack-only narrative fields removed.

    The implementing child agent should never see user names or concrete
    session descriptions, because anything in its context risks leaking
    into PR titles, bodies, or commits. We keep those fields on the
    parent run output (for the internal scorecard) but physically remove
    them before handing the packet to the child. `credit_line` is also
    user-identifying prose ("good catch from Katie"), so it follows the
    same privacy strip.
    """
    return {k: v for k, v in fix.items() if k not in SLACK_ONLY_FIX_FIELDS}


async def _start_fix_children(
    ctx: WorkflowContext,
    *,
    selected_fixes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    children: list[dict[str, Any]] = []
    for index, fix in enumerate(selected_fixes, start=1):
        packet = _strip_slack_only_fields(fix)
        packet["parent_run_id"] = ctx.run_id
        packet["source_threads"] = _normalize_source_threads(packet.get("source_threads"))
        started = await ctx.start_workflow(
            f"selected_fix_{index}",
            workflow_name=WORKFLOW_NAME,
            run_input={
                "mode": "fix_child",
                "fix_packet": packet,
            },
            trigger_key=f"self-improve-fix:{ctx.run_id}:{index}",
            eager_start=True,
        )
        children.append(started)
        ctx.log(
            "self_improve_fix_child_started",
            child_run_id=started.get("run_id"),
            fix_type=packet.get("fix_type"),
            title=packet.get("title"),
        )
    return children


async def _wait_for_fix_children(
    ctx: WorkflowContext,
    children: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index, child in enumerate(children, start=1):
        run_id = str(child.get("run_id") or "")
        try:
            completed = await ctx.wait_for_workflow(
                f"selected_fix_{index}.result",
                run_id=run_id,
                timeout=dt.timedelta(hours=CHILD_TIMEOUT_HOURS),
            )
        except TimeoutError:
            ctx.log(
                "self_improve_fix_child_timeout",
                child_run_id=run_id,
                timeout_hours=CHILD_TIMEOUT_HOURS,
            )
            results.append({
                "child_run_id": run_id,
                "error": f"child workflow timed out after {CHILD_TIMEOUT_HOURS}h",
            })
            continue
        except ControlPlaneError as exc:
            ctx.log(
                "self_improve_fix_child_wait_error",
                child_run_id=run_id,
                error_code=exc.code,
                error_message=exc.message,
            )
            results.append({
                "child_run_id": run_id,
                "error": f"child wait failed: {exc.message}",
            })
            continue

        status = completed.get("status") if isinstance(completed, dict) else None
        error_text = completed.get("error_text") if isinstance(completed, dict) else None
        output_json = completed.get("output_json") if isinstance(completed, dict) else {}
        if isinstance(output_json, str):
            try:
                output_json = json.loads(output_json)
            except (json.JSONDecodeError, TypeError):
                output_json = {"error": "malformed child output", "raw": output_json[:500]}
        if not isinstance(output_json, dict):
            output_json = {}

        if status in {"failed", "cancelled"} and not output_json.get("pr_url"):
            output_json = dict(output_json)
            output_json.setdefault("child_run_id", run_id)
            output_json["error"] = error_text or output_json.get("error") or f"child status: {status}"

        if not output_json:
            output_json = {
                "child_run_id": run_id,
                "error": error_text or "child output was not a JSON object",
            }

        results.append(output_json)
        ctx.log(
            "self_improve_fix_child_completed",
            child_run_id=run_id,
            status=status,
        )
    return results


def _annotate_child_results_with_narratives(
    *,
    child_results: list[dict[str, Any]],
    selected_fixes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Attach each selected fix's slack_narrative to its child result.

    The child workflow never sees `slack_narrative` (we strip it in
    `_start_fix_children` for privacy), but the parent needs it to render
    a human-readable "why we picked this" line next to each PR in the
    scorecard. Children and fixes are paired by position.
    """
    annotated: list[dict[str, Any]] = []
    for index, result in enumerate(child_results):
        entry = dict(result) if isinstance(result, dict) else {}
        fix = selected_fixes[index] if index < len(selected_fixes) else {}
        if isinstance(fix, dict):
            narrative = str(fix.get("slack_narrative") or "").strip()
            if narrative:
                entry["slack_narrative"] = narrative
            credit = str(fix.get("credit_line") or "").strip()
            if credit:
                entry["credit_line"] = credit
            source_threads = fix.get("source_threads")
            if source_threads and not entry.get("source_threads"):
                entry["source_threads"] = source_threads
            # Copy structured framing fields forward so the scorecard can
            # derive concise one-sentence bodies without re-rendering the
            # verbose slack_narrative. The child workflow never sets
            # these, so we always take the fix-packet value when present.
            for key in (
                "dominant_failure_mode",
                "fix_type",
                "title",
                "selection_origin",
                "why_now",
                "what_to_change",
                "what_should_exist",
                "user_value",
            ):
                value = fix.get(key)
                if value and not entry.get(key):
                    entry[key] = value
        annotated.append(entry)
    return annotated


async def _load_recent_fix_contexts(ctx: WorkflowContext) -> list[dict[str, Any]]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=DEDUP_WINDOW_HOURS)
    rows = await ctx._pool.fetch(
        "SELECT output_json FROM workflow_runs "
        "WHERE workflow_name = $1 AND status = 'completed' "
        "  AND created_at >= $2 AND input_json->>'mode' = 'fix_child'",
        WORKFLOW_NAME,
        since,
    )
    seen: set[tuple[str, str, str, str]] = set()
    fixes: list[dict[str, Any]] = []
    for row in rows:
        output = decode_jsonb(dict(row).get("output_json"), {})
        if isinstance(output, dict):
            title = str(output.get("title") or "").strip().lower()
            fix_type = str(output.get("fix_type") or "").strip().lower()
            target_surface = str(output.get("target_surface") or "").strip()
            dominant_failure_mode = str(output.get("dominant_failure_mode") or "").strip().lower()
            if not title:
                continue
            dedup_key = (title, fix_type, target_surface, dominant_failure_mode)
            if dedup_key in seen:
                continue
            seen.add(dedup_key)
            fixes.append(
                {
                    "title": title,
                    "fix_type": fix_type,
                    "target_surface": target_surface,
                    "dominant_failure_mode": dominant_failure_mode,
                    "source_threads": _normalize_source_threads(output.get("source_threads")),
                    "selection_origin": str(output.get("selection_origin") or "").strip(),
                }
            )
    return fixes


async def _run_reconcile_fixes_pass(
    ctx: WorkflowContext,
    *,
    gap_fixes: list[dict[str, Any]],
    build_fixes: list[dict[str, Any]],
    recent_fixes: list[dict[str, Any]],
    max_fixes: int,
) -> list[dict[str, Any]]:
    all_candidates = gap_fixes + build_fixes
    if not all_candidates:
        return []
    if len(all_candidates) == 1 and not recent_fixes:
        return all_candidates

    prompt = textwrap.dedent(
        f"""
        You are reconciling proposed self-improvement fixes from two independent
        analysis passes (gap-analysis and learning-synthesis) before spawning
        child workflows to implement them.

        Your job:
        1. Merge semantically duplicate fixes. Two fixes with the same dominant
           failure mode and overlapping source threads are duplicates even when
           they target different surfaces. Prefer one coherent stack over
           multiple fragmented fixes for the same underlying gap.
        2. Drop any fix that substantially overlaps with a recently attempted fix
           (see recent_fixes below). Compare title, dominant failure mode,
           target surface, AND source thread overlap. Only keep it if you can
           articulate why the prior attempt was insufficient.
        3. Rank the surviving fixes by expected user-value impact.
        4. Return at most {max_fixes} fixes.
        5. Never drop a gap-analysis fix in favor of a learning-synthesis fix
           unless you can clearly justify the tradeoff in `why_now`.

        Each fix in the output must include ALL of these fields:
        `title`, `fix_type`, `target_surface`, `what_to_change`,
        `dominant_failure_mode`, `priority`, `why_now`, `evidence_quotes`,
        `source_threads`, `representative_tasks`, `slack_narrative`.

        `slack_narrative` is a 2-4 sentence human note used internally for
        archival. Always refer to the subject as "Centaur". Name the
        user(s) who surfaced the issue using plain `source_user_name`
        (never `source_user_mention`, never raw Slack IDs), and describe
        concretely what they were trying to do so a human understands
        why this fix was picked. If merging two fixes, synthesize one
        narrative that references both sources. If a fix lacks a
        narrative, write one from the evidence quotes and source threads.

        Also carry through `credit_line` — an OPTIONAL short warm sentence
        (≤120 chars) crediting the user for a specific good ask or
        observation, using plain `source_user_name` only. When merging
        two fixes, pick the stronger credit line or synthesize one if
        both sources credibly support one. When in doubt, leave it empty
        and the scorecard will fall back to a plain name.

        If a field was present in the input fix, preserve it. If you merge two
        fixes, combine their evidence and source threads. Preserve
        `selection_origin` when present; if you merge a gap fix with a learning
        fix, set `selection_origin` to `mixed`.

        Return JSON only with exactly this top-level key:
        - `reconciled_fixes`: array of fix objects (at most {max_fixes}).

        Recently attempted fixes (skip these unless clearly insufficient):
        ```json
        {json.dumps(recent_fixes, indent=2)}
        ```

        Candidate fixes from gap-analysis:
        ```json
        {json.dumps(gap_fixes, indent=2)}
        ```

        Candidate fixes from learning-synthesis:
        ```json
        {json.dumps(build_fixes, indent=2)}
        ```
        """
    ).strip()

    reconcile_turn = await ctx.agent_turn(
        prompt,
        thread_key=f"workflow:{ctx.run_id}:reconcile-fixes",
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "parent",
            "stage": "reconcile_fixes",
        },
    )

    async def _parse_reconcile() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(reconcile_turn.get("result_text") or ""),
            stage="reconcile_fixes",
            preferred_keys=RECONCILE_PREFERRED_KEYS,
            required_keys=RECONCILE_REQUIRED_KEYS,
        )

    reconciled = await ctx.step("reconcile_fixes", _parse_reconcile, step_kind="review")
    fixes = list(reconciled.get("reconciled_fixes") or [])
    for fix in fixes:
        if isinstance(fix, dict):
            fix["source_threads"] = _normalize_source_threads(fix.get("source_threads"))
    ctx.log(
        "self_improve_reconcile_completed",
        input_count=len(all_candidates),
        output_count=len(fixes),
        recent_titles_count=len(recent_fixes),
    )
    return [fix for fix in fixes if isinstance(fix, dict)][:max_fixes]


CENTAUR_REPO = _resolve_current_repo()

# Allow-list for auto-merge: these are the only paths a self-improve PR
# may touch for the squash-merge gate to fire. Anything outside this set
# (platform code, services, migrations, workflows, infra) is left open
# for human review no matter how clean the diff looks.
_AUTO_MERGE_PATH_PREFIXES = (".agents/skills/",)
_AUTO_MERGE_PERSONA_PREFIX = "tools/personas/"
_AUTO_MERGE_PERSONA_SUFFIXES = ("PROMPT.md", "pyproject.toml")
_AUTO_MERGE_SANDBOX_PREFIX = "services/sandbox/SYSTEM_PROMPT"

_AUTO_MERGE_SAFE_FIX_TYPES = frozenset({"prompt_tweak", "new_skill", "new_persona"})


def _is_auto_merge_safe_path(path: str) -> bool:
    """Return True when *path* matches the auto-merge allow-list.

    We check concrete, human-reviewable prefixes instead of a blanket
    "any Markdown file" rule because the cost of auto-merging platform
    code is far higher than the cost of leaving a safe PR open one more
    minute for a human to click Approve.
    """
    p = str(path or "").strip()
    if not p:
        return False
    if any(p.startswith(prefix) for prefix in _AUTO_MERGE_PATH_PREFIXES):
        return True
    if p.startswith(_AUTO_MERGE_PERSONA_PREFIX) and any(
        p.endswith(suffix) for suffix in _AUTO_MERGE_PERSONA_SUFFIXES
    ):
        return True
    if p.startswith(_AUTO_MERGE_SANDBOX_PREFIX):
        return True
    return False


def _validation_has_failing_check(validation: Any) -> bool:
    """Return True when a child's validation reports any failing check.

    Accepts several shapes because the child agent produces
    free-form JSON; we look defensively for `passed: false`, a status
    in {fail, failed, error, false}, or success booleans flipped off.
    When in doubt we assume the PR is NOT safe to auto-merge.
    """
    if not isinstance(validation, dict):
        return False
    checks = validation.get("checks")
    if not isinstance(checks, list):
        return False
    for check in checks:
        if not isinstance(check, dict):
            continue
        if check.get("passed") is False:
            return True
        status = str(check.get("status") or check.get("result") or "").strip().lower()
        if status in {"fail", "failed", "error", "false"}:
            return True
        if check.get("ok") is False or check.get("success") is False:
            return True
    return False


_GITHUB_API_ROOT = "https://api.github.com"
_GITHUB_API_TIMEOUT_S = 15.0


def _github_auth_headers() -> dict[str, str]:
    """Return GitHub REST headers with the API server's token.

    The API container sets ``GITHUB_TOKEN`` in its entrypoint (see
    `services/api/entrypoint.sh`). When the token is missing we still
    return the accept header and let GitHub respond with 401; every
    caller treats non-2xx as "leave the PR open / drop the clause".
    """
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    token = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or ""
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


async def _count_merged_self_improve_prs_24h(ctx: WorkflowContext) -> int:
    """Count self-improve PRs merged in the last 24 hours, via GitHub API.

    We can't use `self_improve_deploy_notifier.output_json` for this any
    more: the notifier fires on deploy events that land AFTER the 22:00
    PT nightly, so its 24h window is structurally empty from the
    nightly's vantage point. Querying the GitHub search API directly
    gives us the authoritative count. On any failure we return -1 and
    the scorecard simply omits the clause — never zero-pads an
    unreliable number.
    """

    if not CENTAUR_REPO:
        ctx.log("self_improve_repo_unset_for_merge_count")
        return -1

    async def _count() -> int:
        since_iso = (
            dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=24)
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        query = (
            f"repo:{CENTAUR_REPO} is:pr is:merged label:self-improve "
            f"merged:>={since_iso}"
        )
        try:
            import httpx

            async with httpx.AsyncClient(
                follow_redirects=True, timeout=_GITHUB_API_TIMEOUT_S
            ) as client:
                resp = await client.get(
                    f"{_GITHUB_API_ROOT}/search/issues",
                    params={"q": query, "per_page": 1},
                    headers=_github_auth_headers(),
                )
        except Exception as exc:
            ctx.log("self_improve_merged_24h_count_network_error", error=str(exc))
            return -1
        if resp.status_code != 200:
            ctx.log(
                "self_improve_merged_24h_count_failed",
                status=resp.status_code,
                body=resp.text[:500],
            )
            return -1
        try:
            return int(resp.json().get("total_count") or 0)
        except (ValueError, TypeError, KeyError):
            return -1

    try:
        return int(
            await ctx.step(
                "count_merged_self_improve_prs_24h", _count, step_kind="tool_call"
            )
        )
    except (SuspendWorkflow, CancelledWorkflow, NonRetryableError):
        raise
    except Exception as exc:
        ctx.log("self_improve_merged_24h_count_exception", error=str(exc))
        return -1


async def _auto_merge_safe_children(
    ctx: WorkflowContext,
    child_results: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Annotate each child result with `auto_merge_status` and optionally merge.

    Gate: a child result is merged only when ALL of these hold:
      - `pr_number` and `pr_url` are present and there is no `error`
      - `fix_type` is in the safe set (prompt_tweak, new_skill, new_persona)
      - every changed file (queried from GitHub) is on the
        prompt/skill/persona allow-list
      - `validation.checks` contains no failing entry

    Every per-PR operation is wrapped in try/except so an unexpected
    failure never blocks the scorecard post downstream. `auto_merge_status`
    lands in one of {"merged", "skipped_by_policy", "failed"}; the
    scorecard uses only "merged" to populate the *Shipped tonight*
    section. Control-flow exceptions (``SuspendWorkflow``,
    ``CancelledWorkflow``, ``NonRetryableError``) propagate so the
    workflow engine can suspend / cancel / fail cleanly.
    """
    out: list[dict[str, Any]] = []
    for index, entry in enumerate(child_results):
        if not isinstance(entry, dict):
            out.append(entry)
            continue
        annotated = dict(entry)
        try:
            status, reason = await _auto_merge_one_child(ctx, entry, index)
            annotated["auto_merge_status"] = status
            if reason:
                annotated["auto_merge_reason"] = reason
        except (SuspendWorkflow, CancelledWorkflow, NonRetryableError):
            raise
        except Exception as exc:
            ctx.log(
                "self_improve_auto_merge_unexpected_error",
                child_index=index,
                pr_number=entry.get("pr_number"),
                error=str(exc),
            )
            annotated["auto_merge_status"] = "failed"
            annotated["auto_merge_reason"] = f"unexpected error: {exc}"
        out.append(annotated)
    return out


async def _github_list_pr_files(pr_number: int | str) -> list[str]:
    """Return the list of changed file paths for *pr_number* in centaur.

    Uses the GitHub REST API (not the `gh` CLI) because the API server
    container doesn't ship `gh`. Raises on any non-2xx response so the
    caller can mark the PR auto_merge_failed rather than mistakenly
    merge a PR with unknown file diff.
    """
    import httpx

    if not CENTAUR_REPO:
        raise RuntimeError("GitHub repo is not configured")

    files: list[str] = []
    page = 1
    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_GITHUB_API_TIMEOUT_S
    ) as client:
        while page <= 5:  # Cap: a safe self-improve PR should be tiny.
            resp = await client.get(
                f"{_GITHUB_API_ROOT}/repos/{CENTAUR_REPO}/pulls/{pr_number}/files",
                params={"per_page": 100, "page": page},
                headers=_github_auth_headers(),
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"GitHub PR files returned {resp.status_code}: "
                    f"{resp.text[:200]}"
                )
            batch = resp.json()
            if not isinstance(batch, list):
                raise RuntimeError(
                    f"GitHub PR files returned non-list body: {str(batch)[:200]}"
                )
            for item in batch:
                if isinstance(item, dict):
                    path = str(item.get("filename") or "").strip()
                    if path:
                        files.append(path)
            if len(batch) < 100:
                break
            page += 1
    return files


async def _github_get_pr_metadata(pr_number: int | str) -> dict[str, Any]:
    """Return basic PR metadata needed for auto-merge.

    We need the PR node id for the GraphQL `enablePullRequestAutoMerge`
    mutation. The REST `GET /pulls/{number}` endpoint returns it as
    `node_id`, along with `mergeable_state` and the current head SHA for
    safer enablement.
    """
    import httpx

    if not CENTAUR_REPO:
        raise RuntimeError("GitHub repo is not configured")

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_GITHUB_API_TIMEOUT_S
    ) as client:
        resp = await client.get(
            f"{_GITHUB_API_ROOT}/repos/{CENTAUR_REPO}/pulls/{pr_number}",
            headers=_github_auth_headers(),
        )
    if resp.status_code != 200:
        raise RuntimeError(
            f"GitHub PR metadata returned {resp.status_code}: {resp.text[:200]}"
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise RuntimeError(f"GitHub PR metadata returned non-dict body: {str(body)[:200]}")
    node_id = str(body.get("node_id") or "").strip()
    head_sha = str((body.get("head") or {}).get("sha") or "").strip()
    mergeable_state = str(body.get("mergeable_state") or "").strip()
    if not node_id:
        raise RuntimeError("GitHub PR metadata missing node_id")
    return {
        "node_id": node_id,
        "head_sha": head_sha,
        "mergeable_state": mergeable_state,
    }


async def _github_enable_auto_merge(pr_number: int | str) -> dict[str, Any]:
    """Enable squash auto-merge on *pr_number* via the GitHub GraphQL API.

    This is the behavior the original plan called for:
    `gh pr merge --squash --auto`. Unlike the immediate REST merge
    endpoint, the GraphQL auto-merge mutation succeeds even when checks
    are still finishing, and GitHub merges the PR once protections are
    satisfied.

    We treat successful enablement as `merged` for scorecard purposes,
    matching the original `--auto` semantics: the safe PR is now queued
    to land without further human action.
    """
    import httpx

    metadata = await _github_get_pr_metadata(pr_number)
    mutation = """
    mutation EnableAutoMerge($pullRequestId: ID!, $expectedHeadOid: GitObjectID) {
      enablePullRequestAutoMerge(
        input: {
          pullRequestId: $pullRequestId
          mergeMethod: SQUASH
          expectedHeadOid: $expectedHeadOid
        }
      ) {
        pullRequest {
          number
          autoMergeRequest {
            enabledAt
          }
        }
      }
    }
    """

    async with httpx.AsyncClient(
        follow_redirects=True, timeout=_GITHUB_API_TIMEOUT_S
    ) as client:
        resp = await client.post(
            f"{_GITHUB_API_ROOT}/graphql",
            json={
                "query": mutation,
                "variables": {
                    "pullRequestId": metadata["node_id"],
                    "expectedHeadOid": metadata["head_sha"] or None,
                },
            },
            headers=_github_auth_headers(),
        )
    if resp.status_code == 200:
        body = resp.json() if resp.content else {}
        if not isinstance(body, dict):
            raise RuntimeError(f"GitHub auto-merge returned non-dict body: {str(body)[:200]}")
        errors = body.get("errors")
        if isinstance(errors, list) and errors:
            message = "; ".join(
                str(err.get("message") or "") for err in errors if isinstance(err, dict)
            ).strip()
            lowered = message.lower()
            # Idempotent success: another replay or human may have already
            # enabled auto-merge on this PR.
            if "already has auto-merge enabled" in lowered:
                return {
                    "status": "auto_merge_enabled",
                    "message": message,
                    "mergeable_state": metadata["mergeable_state"],
                }
            raise RuntimeError(f"GitHub auto-merge errors: {message[:200]}")
        return {
            "status": "auto_merge_enabled",
            "message": "auto-merge enabled",
            "mergeable_state": metadata["mergeable_state"],
        }
    raise RuntimeError(
        f"GitHub auto-merge returned {resp.status_code}: {resp.text[:200]}"
    )


async def _auto_merge_one_child(
    ctx: WorkflowContext,
    entry: dict[str, Any],
    index: int,
) -> tuple[str, str]:
    """Run the auto-merge gate for one child result.

    Returns ``(status, reason)`` where status is one of
    {"merged", "skipped_by_policy", "failed"}. The caller owns putting
    these on the child result; this function owns the gate logic.
    Control-flow exceptions propagate up so the workflow engine can
    suspend / cancel / fail cleanly.
    """
    pr_number = entry.get("pr_number")
    pr_url = str(entry.get("pr_url") or "").strip()
    error_text = str(entry.get("error") or "").strip()

    if error_text or not pr_number or not pr_url:
        return "skipped_by_policy", "no PR or child reported error"

    fix_type = str(entry.get("fix_type") or "").strip().lower()
    if fix_type not in _AUTO_MERGE_SAFE_FIX_TYPES:
        return "skipped_by_policy", f"fix_type {fix_type!r} not in safe set"

    if _validation_has_failing_check(entry.get("validation")):
        return "skipped_by_policy", "validation reported a failing check"

    # Fetch the list of changed files directly from GitHub so we don't
    # trust the child agent's self-reported `changed_files`. Each call
    # is checkpointed so replays don't re-run the network call.
    pr_key = str(pr_number).strip() or f"index{index}"

    async def _fetch_files() -> list[str]:
        return await _github_list_pr_files(pr_number)

    try:
        files = await ctx.step(
            f"auto_merge_files_{pr_key}", _fetch_files, step_kind="tool_call"
        )
    except (SuspendWorkflow, CancelledWorkflow, NonRetryableError):
        raise
    except Exception as exc:
        return "failed", f"could not list changed files: {exc}"

    if not files:
        return "skipped_by_policy", "GitHub returned no changed files"
    unsafe = [path for path in files if not _is_auto_merge_safe_path(path)]
    if unsafe:
        return (
            "skipped_by_policy",
            f"{len(unsafe)} file(s) outside the allow-list (e.g. {unsafe[0]})",
        )

    async def _merge() -> dict[str, Any]:
        return await _github_enable_auto_merge(pr_number)

    try:
        result = await ctx.step(
            f"auto_merge_merge_{pr_key}", _merge, step_kind="tool_call"
        )
    except (SuspendWorkflow, CancelledWorkflow, NonRetryableError):
        raise
    except Exception as exc:
        return "failed", f"merge API raised: {exc}"

    ctx.log(
        "self_improve_auto_merge_merged",
        pr_number=pr_number,
        fix_type=fix_type,
        changed_file_count=len(files),
        sha=result.get("sha", "") if isinstance(result, dict) else "",
    )
    return "merged", ""


async def _run_parent(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    user_name_by_id = await _fetch_user_name_cache(ctx)
    review_window_hours = max(inp.review_window_hours, 1)

    async def _load_window_counts() -> dict[str, int]:
        return await _load_review_window_counts(
            ctx,
            review_window_hours=review_window_hours,
        )

    async def _collect() -> list[dict[str, Any]]:
        return await _collect_evidence_packs(
            ctx,
            review_window_hours=review_window_hours,
            candidate_limit=max(inp.candidate_limit, 1),
            user_name_by_id=user_name_by_id,
        )

    coverage = await ctx.step("load_review_window_counts", _load_window_counts, step_kind="gather")
    all_tasks = await ctx.step("collect_tasks", _collect, step_kind="gather")

    async def _validate_names() -> dict[str, Any]:
        tasks, stats = _validate_source_user_names(
            list(all_tasks),
            user_name_by_id=user_name_by_id,
        )
        return {"tasks": tasks, "stats": stats}

    validated = await ctx.step("validate_source_user_names", _validate_names, step_kind="gather")
    if isinstance(validated, dict):
        all_tasks = list(validated.get("tasks") or [])
        name_stats = dict(validated.get("stats") or {})
    else:
        name_stats = {}

    coverage = dict(coverage or {})
    coverage["reconstructed_task_count"] = len(all_tasks)
    coverage["reconstructed_thread_count"] = len(
        {
            str(task.get("thread_key") or "").strip()
            for task in all_tasks
            if isinstance(task, dict) and str(task.get("thread_key") or "").strip()
        }
    )
    coverage["source_user_name_batch_complete"] = bool(name_stats.get("complete", True))
    coverage["source_user_name_hydrated_count"] = int(name_stats.get("hydrated_count") or 0)
    coverage["source_user_name_normalized_count"] = int(name_stats.get("normalized_count") or 0)
    coverage["source_user_name_missing_count"] = int(name_stats.get("missing_name_count") or 0)
    coverage["batch_complete"] = bool(name_stats.get("complete", True))

    unresolved_user_ids = list(name_stats.get("unresolved_user_ids") or [])
    ctx.log(
        "self_improve_source_user_names_validated",
        hydrated_count=coverage["source_user_name_hydrated_count"],
        normalized_count=coverage["source_user_name_normalized_count"],
        missing_name_count=coverage["source_user_name_missing_count"],
        complete=coverage["source_user_name_batch_complete"],
    )
    if unresolved_user_ids:
        ctx.log(
            "self_improve_source_user_names_unresolved",
            count=len(unresolved_user_ids),
            unresolved_user_ids=unresolved_user_ids[:25],
        )
    ctx.log("self_improve_batch_collected", tasks_total=len(all_tasks), **coverage)

    if inp.candidate_limit > 0:
        selected_ids = await _run_triage_pass(
            ctx,
            tasks=all_tasks,
            limit=max(inp.candidate_limit, 1),
        )
        id_set = set(selected_ids)
        evidence_packs = [
            task for task in all_tasks if str(task.get("task_id") or "") in id_set
        ]
        if not evidence_packs:
            evidence_packs = all_tasks[:max(inp.candidate_limit, 1)]
    else:
        evidence_packs = list(all_tasks)
    ctx.log(
        "self_improve_triage_applied",
        total_tasks=len(all_tasks),
        selected_tasks=len(evidence_packs),
    )
    coverage["triaged_task_count"] = len(evidence_packs)

    async def _load_dedup_contexts() -> list[dict[str, Any]]:
        return await _load_recent_fix_contexts(ctx)

    selection_limit = _selection_limit(inp.max_selected_fixes, len(all_tasks))
    recent_fixes = list(
        await ctx.step("load_recent_fix_contexts", _load_dedup_contexts, step_kind="gather")
    )
    recent_titles = [
        str(item.get("title") or "").strip()
        for item in recent_fixes
        if isinstance(item, dict) and str(item.get("title") or "").strip()
    ]

    review_batches = [
        await _run_batch_review_pass(
            ctx,
            evidence_packs=batch,
            max_selected_fixes=selection_limit,
            recent_fix_titles=recent_titles,
            batch_tag=f"chunk-{index}",
        )
        for index, batch in enumerate(
            _chunk_tasks(evidence_packs, max(inp.review_batch_size, 1)),
            start=1,
        )
    ]
    review = _merge_review_batches(review_batches, tasks_reviewed=len(evidence_packs))

    synthesis = await _run_learning_synthesis_pass(
        ctx,
        evidence_packs=evidence_packs,
        max_selected_builds=selection_limit,
    )
    if isinstance(synthesis, dict):
        synthesis["selected_builds"] = _augment_builds_with_related_window_threads(
            list(synthesis.get("selected_builds") or []),
            all_tasks,
        )
    ctx.log(
        "self_improve_learning_synthesis",
        opportunities_found=synthesis.get("opportunities_found", 0),
        selected_builds=len(list(synthesis.get("selected_builds") or [])),
    )

    gap_fixes = []
    for item in list(review.get("selected_fixes") or []):
        if not isinstance(item, dict):
            continue
        fix = dict(item)
        fix.setdefault("selection_origin", "gap_analysis")
        gap_fixes.append(fix)
    build_fixes = []
    for build in list(synthesis.get("selected_builds") or []):
        if not isinstance(build, dict):
            continue
        raw_fix_type = str(build.get("opportunity_type") or "new_skill")
        fix_type = "tool_improvement" if raw_fix_type == "new_tool_idea" else raw_fix_type
        build_fixes.append({
            "title": build.get("title", ""),
            "fix_type": fix_type,
            "target_surface": build.get("target_surface", ""),
            "what_to_change": build.get("implementation_sketch", ""),
            "dominant_failure_mode": f"learning: {raw_fix_type}",
            "priority": "medium",
            "why_now": build.get("user_value", ""),
            "evidence_quotes": [build.get("evidence_summary", "")],
            "source_threads": build.get("source_threads", []),
            "representative_tasks": [],
            "new_capability_justification": build.get("what_should_exist", ""),
            "slack_narrative": build.get("slack_narrative", ""),
            "selection_origin": "learning_synthesis",
        })

    selected_fixes = await _run_reconcile_fixes_pass(
        ctx,
        gap_fixes=gap_fixes,
        build_fixes=build_fixes,
        recent_fixes=recent_fixes,
        max_fixes=_selection_limit(inp.max_selected_fixes, len(gap_fixes) + len(build_fixes)),
    )

    children = await _start_fix_children(ctx, selected_fixes=selected_fixes)
    child_results = await _wait_for_fix_children(ctx, children)
    child_results = _annotate_child_results_with_narratives(
        child_results=child_results,
        selected_fixes=selected_fixes,
    )

    child_results = await _auto_merge_safe_children(ctx, child_results)
    auto_merge_summary = {
        "merged": sum(
            1
            for entry in child_results
            if isinstance(entry, dict)
            and str(entry.get("auto_merge_status") or "") == "merged"
        ),
        "skipped": sum(
            1
            for entry in child_results
            if isinstance(entry, dict)
            and str(entry.get("auto_merge_status") or "") == "skipped_by_policy"
        ),
        "failed": sum(
            1
            for entry in child_results
            if isinstance(entry, dict)
            and str(entry.get("auto_merge_status") or "") == "failed"
        ),
    }
    ctx.log("self_improve_auto_merge_summary", **auto_merge_summary)

    # Per-thread user name lookup used only by the scorecard renderer
    # to attribute fixes/builds without leaking raw `<@U...>` IDs.
    thread_user_names = {
        str(task.get("thread_key") or "").strip(): str(task.get("source_user_name") or "").strip()
        for task in all_tasks
        if isinstance(task, dict)
        and str(task.get("thread_key") or "").strip()
        and str(task.get("source_user_name") or "").strip()
    }

    merged_prs_24h = await _count_merged_self_improve_prs_24h(ctx)

    # Compact digest of tonight's threads — only used by the polish
    # pass to anchor the flair line in a real topic or team pattern.
    flair_digest = _build_flair_digest(all_tasks)

    polish = await _polish_scorecard_bullets(
        ctx,
        review=review,
        synthesis=synthesis,
        child_results=child_results,
        flair_digest=flair_digest,
    )
    polished_bodies = polish.get("bodies") if isinstance(polish, dict) else {}
    polished_flair = polish.get("flair") if isinstance(polish, dict) else ""

    coverage["reviewed_task_count"] = int(review.get("tasks_reviewed") or len(evidence_packs))
    ctx.log("self_improve_coverage_summary", **coverage)
    scorecard = _build_scorecard_markdown(
        review=review,
        synthesis=synthesis,
        child_results=child_results,
        coverage=coverage,
        thread_user_names=thread_user_names,
        merged_prs_24h=merged_prs_24h if merged_prs_24h >= 0 else None,
        polished_bodies=polished_bodies if isinstance(polished_bodies, dict) else {},
        polished_flair=str(polished_flair or ""),
    )

    # Try the new #ai-agent channel first; if the bot isn't in it or
    # anything else blocks the post, fall back to #ai-v2 (the prior
    # destination we know works) so the nightly never silently
    # disappears. `post_to_slack` is a checkpointed step, so on replay
    # the fallback won't double-post.
    posted_channel = "ai-agent"
    try:
        await ctx.post_to_slack("ai-agent", scorecard)
    except (SuspendWorkflow, CancelledWorkflow, NonRetryableError):
        raise
    except Exception as exc:
        ctx.log(
            "self_improve_scorecard_ai_agent_post_failed",
            error=str(exc),
            fallback="ai-v2",
        )
        posted_channel = "ai-v2"
        await ctx.post_to_slack("ai-v2", scorecard)
    ctx.log(
        "self_improve_scorecard_posted",
        channel=posted_channel,
        tasks_reviewed=review.get("tasks_reviewed", 0),
        below_bar_count=review.get("below_bar_count", 0),
        selected_fix_count=len(selected_fixes),
        opportunities_found=synthesis.get("opportunities_found", 0),
        merged_prs_24h=merged_prs_24h,
        **auto_merge_summary,
    )
    return {
        "mode": "parent",
        "review": review,
        "synthesis": synthesis,
        "selected_fixes": selected_fixes,
        "opened_prs": child_results,
        "coverage": coverage,
        "auto_merge_summary": auto_merge_summary,
        "merged_prs_24h": merged_prs_24h,
        "scorecard": scorecard,
    }


async def _run_phase(
    ctx: WorkflowContext,
    *,
    phase_name: str,
    agent_thread_key: str,
    prompt: str,
    preferred_keys: tuple[str, ...] = (),
    required_keys: tuple[str, ...] = (),
) -> dict[str, Any]:
    # Use a phase-specific message_id so multiple agent_turn calls on the
    # same thread_key don't collide on the idempotency check in append_message.
    phase_message_id = f"wf:{ctx.run_id}:{phase_name}:message"
    phase_result = await ctx.agent_turn(
        prompt,
        thread_key=agent_thread_key,
        message_id=phase_message_id,
        delivery=Delivery.dev(),
        prompt_selector="eng",
        metadata={
            "source": WORKFLOW_NAME,
            "mode": "fix_child",
            "phase": phase_name,
        },
    )

    async def _parse_phase() -> dict[str, Any]:
        return _extract_required_json_payload(
            str(phase_result.get("result_text") or ""),
            stage=phase_name,
            preferred_keys=preferred_keys,
            required_keys=required_keys,
        )

    return await ctx.step(phase_name, _parse_phase, step_kind="phase")


async def _run_fix_child(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    fix_packet = dict(inp.fix_packet or {})
    if not fix_packet:
        raise ControlPlaneError(
            "INVALID_WORKFLOW_INPUT",
            "fix_child mode requires fix_packet",
            422,
        )

    fix_packet["source_threads"] = _normalize_source_threads(fix_packet.get("source_threads"))
    fix_type = fix_packet.get("fix_type", "unknown")
    git_branch_instruction = (
        f"Use `git-branch {CENTAUR_REPO}` at the start because the mounted repo is read-only."
        if CENTAUR_REPO
        else "Before editing, resolve the current repository slug with `git remote get-url origin`, then run `git-branch <owner/repo>` because the mounted repo is read-only."
    )

    # Run research → plan → implement → validate → open_pr as one agent turn.
    # This is a single conversation on one sandbox, so git branches, edits, and
    # validations all share filesystem state through PR creation.
    execute_prompt = textwrap.dedent(
        f"""
        Load the `improve-gap-task` skill first.

        You are implementing one selected self-improvement fix end-to-end in one
        session. Work through all five phases in order, using tool calls between
        phases so the filesystem and git state stay consistent:

        1. research — understand the fix and its surface area
        2. plan — produce a concrete plan for one focused PR
        3. implement — apply the change in the writable clone
        4. validate — run the smallest relevant checks
        5. open PR — commit, push, and open the PR

        {git_branch_instruction} Keep the change tightly scoped to one focused PR.

        If the fix type is `new_skill` or `new_persona`, include an explicit
        justification for why this is a missing-capability problem rather than
        a code, workflow, prompt, or tool fix.

        The PR must include labels `self-improve` and `fix-type:{fix_type}`.

        Write the PR like a senior engineer. The body must have:
        Summary (1-3 bullets), Problem (root cause in system terms),
        Fix (what changed and why), Verification (checks run).

        CRITICAL privacy rule: the PR title, body, and commits must NEVER
        contain user names, Slack handles, thread URLs, task IDs, or any
        content from specific user conversations. Describe system behavior
        patterns, not individual sessions.

        After opening the PR, verify with `gh pr view` that the required
        labels (`self-improve` and `fix-type:{fix_type}`) are present on the
        PR. Fix the PR if verification fails. Do NOT add any hidden HTML
        comment metadata block — labels alone identify self-improve PRs.

        Return JSON only with these top-level keys:
        - `research`: object with root_cause, fix_type, affected_files,
          acceptance_criteria, verification_plan, risks, confidence
        - `plan`: object with files, plan, validation, pr_title, expected_impact
        - `changed_files`: array of edited file paths
        - `validation`: object with checks (array of command+status), summary,
          regression_check
        - `branch`: the branch name you pushed
        - `commit`: the commit sha
        - `pr_number`: the created PR number
        - `pr_url`: the created PR URL
        - `pr_title`: the final PR title
        - `verified_handoff`: true if `gh pr view` confirmed labels + metadata

        Fix packet:
        ```json
        {json.dumps(fix_packet, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()

    execute_result = await _run_phase(
        ctx,
        phase_name="execute_fix",
        agent_thread_key=f"workflow:{ctx.run_id}:fix",
        prompt=execute_prompt,
        preferred_keys=EXECUTE_PREFERRED_KEYS,
        required_keys=EXECUTE_REQUIRED_KEYS,
    )
    ctx.log(
        "self_improve_fix_phase",
        phase="execute_fix",
        fix_type=fix_type,
        pr_number=execute_result.get("pr_number"),
    )

    plan_out = execute_result.get("plan") if isinstance(execute_result.get("plan"), dict) else {}
    return {
        "mode": "fix_child",
        "title": fix_packet.get("title"),
        "fix_type": fix_type,
        "source_threads": fix_packet.get("source_threads", []),
        "research": execute_result.get("research"),
        "plan": plan_out,
        "implementation": {
            "changed_files": execute_result.get("changed_files", []),
            "summary": execute_result.get("pr_title", ""),
        },
        "validation": execute_result.get("validation"),
        "pr_number": execute_result.get("pr_number"),
        "pr_url": execute_result.get("pr_url"),
        "branch": execute_result.get("branch"),
        "title_draft": execute_result.get("pr_title") or plan_out.get("pr_title"),
        "verified_handoff": bool(execute_result.get("verified_handoff", False)),
    }


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    if inp.mode == "parent":
        return await _run_parent(inp, ctx)
    if inp.mode == "fix_child":
        return await _run_fix_child(inp, ctx)
    raise ControlPlaneError(
        "INVALID_WORKFLOW_INPUT",
        f"unsupported mode: {inp.mode}",
        422,
    )
