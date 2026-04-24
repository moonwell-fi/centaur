"""Workflow: reply in original affected Slack threads once self-improvement fixes are live."""

from __future__ import annotations

import datetime as dt
import hashlib
import os
import json
import textwrap
from dataclasses import dataclass, field
from typing import Any

from api.runtime_control import ControlPlaneError, decode_jsonb
from api.workflow_engine import Delivery, WorkflowContext
from workflows.json_payloads import extract_json_payload

WORKFLOW_NAME = "self_improve_deploy_notifier"
NOTIFIED_REPLY_WINDOW_HOURS = 24 * 180


@dataclass
class Input:
    before_sha: str = ""
    after_sha: str = ""
    baseline_sha: str = ""
    repo: str = field(default_factory=lambda: os.getenv("SELF_IMPROVE_REPO") or os.getenv("GITHUB_REPOSITORY") or "")
    status: str = "success"
    deployed_at: str = ""
    merged_prs: list[dict[str, Any]] = field(default_factory=list)
    notifications: list[dict[str, Any]] = field(default_factory=list)


def _parse_thread_key(thread_key: str) -> tuple[str, str]:
    parts = thread_key.strip().split(":")
    if len(parts) == 2 and parts[0] and parts[1]:
        return parts[0], parts[1]
    if len(parts) == 3 and parts[1] and parts[2]:
        return parts[1], parts[2]
    raise ValueError(f"invalid thread key: {thread_key}")


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _normalize_notification(entry: dict[str, Any]) -> dict[str, Any] | None:
    pr_number = _safe_int(entry.get("pr_number"))
    pr_url = str(entry.get("pr_url") or "").strip()
    thread_key = str(entry.get("thread_key") or "").strip()
    channel = str(entry.get("channel") or "").strip()
    thread_ts = str(entry.get("thread_ts") or "").strip()
    summary = str(entry.get("summary") or "This improvement").strip()
    if not channel and not thread_ts and thread_key:
        try:
            channel, thread_ts = _parse_thread_key(thread_key)
        except ValueError:
            return None
    if not thread_key and channel and thread_ts:
        thread_key = f"{channel}:{thread_ts}"
    if not pr_number or not pr_url or not thread_key or not channel or not thread_ts:
        return None
    return {
        "pr_number": pr_number,
        "pr_url": pr_url,
        "thread_key": thread_key,
        "channel": channel,
        "thread_ts": thread_ts,
        "summary": summary,
    }


def _dedupe_notifications(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen_pairs: set[tuple[int, str]] = set()
    for entry in entries:
        normalized = _normalize_notification(entry)
        if normalized is None:
            continue
        pair = (int(normalized["pr_number"]), str(normalized["thread_key"]))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        deduped.append(normalized)
    return deduped


def _filter_already_notified(
    entries: list[dict[str, Any]],
    *,
    already_notified: set[tuple[int, str]],
) -> list[dict[str, Any]]:
    if not already_notified:
        return entries
    filtered: list[dict[str, Any]] = []
    for entry in entries:
        pair = (int(entry["pr_number"]), str(entry["thread_key"]))
        if pair in already_notified:
            continue
        filtered.append(entry)
    return filtered


def _reply_step_name(pr_number: int, thread_key: str) -> str:
    digest = hashlib.sha1(f"{pr_number}:{thread_key}".encode("utf-8")).hexdigest()[:10]
    return f"reply_{pr_number}_{digest}"


def _build_default_reply(entry: dict[str, Any]) -> str:
    return f"This improvement is live now: {entry['summary']}. PR: {entry['pr_url']}"


def _normalize_reply_drafts(payload: dict[str, Any]) -> dict[tuple[int, str], str]:
    drafts: dict[tuple[int, str], str] = {}
    replies = payload.get("replies") if isinstance(payload, dict) else None
    if not isinstance(replies, list):
        return drafts
    for entry in replies:
        if not isinstance(entry, dict):
            continue
        pr_number = _safe_int(entry.get("pr_number"))
        thread_key = str(entry.get("thread_key") or "").strip()
        message = str(entry.get("message") or "").strip()
        if pr_number and thread_key and message:
            drafts[(pr_number, thread_key)] = message
    return drafts


async def _load_recent_notified_pairs(ctx: WorkflowContext) -> set[tuple[int, str]]:
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=NOTIFIED_REPLY_WINDOW_HOURS)
    rows = await ctx._pool.fetch(
        "SELECT c.state FROM workflow_checkpoints c "
        "JOIN workflow_runs r ON r.run_id = c.run_id "
        "WHERE r.workflow_name = $1 AND c.step_kind = 'slack_post' "
        "  AND c.created_at >= $2",
        WORKFLOW_NAME,
        since,
    )
    seen_pairs: set[tuple[int, str]] = set()
    for row in rows:
        state = decode_jsonb(dict(row).get("state"), {})
        if not isinstance(state, dict):
            continue
        pr_number = _safe_int(state.get("pr_number"))
        thread_key = str(state.get("thread_key") or "").strip()
        if pr_number and thread_key:
            seen_pairs.add((pr_number, thread_key))
    return seen_pairs


async def _draft_thread_replies(
    ctx: WorkflowContext,
    *,
    notifications: list[dict[str, Any]],
) -> dict[tuple[int, str], str]:
    if not notifications:
        return {}

    async def _enrich() -> list[dict[str, Any]]:
        from api.app import get_tool_manager

        result = []
        for entry in notifications:
            enriched_entry = dict(entry)
            channel = str(entry.get("channel") or "").strip()
            thread_ts = str(entry.get("thread_ts") or "").strip()
            if channel and thread_ts:
                try:
                    tm = get_tool_manager()
                    raw = await tm.call_tool(
                        "slack",
                        "get_thread_replies",
                        {"channel_id": channel, "thread_ts": thread_ts, "limit": 20},
                    )
                    thread_data = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(thread_data, list):
                        enriched_entry["thread_messages"] = [
                            {
                                "user": str(m.get("user") or ""),
                                "text": str(m.get("text") or "")[:300],
                            }
                            for m in thread_data[:15]
                            if isinstance(m, dict) and str(m.get("text") or "").strip()
                        ]
                except Exception:
                    ctx.log(
                        "self_improve_notifier_thread_context_fetch_failed",
                        channel=channel,
                        thread_ts=thread_ts,
                    )
            result.append(enriched_entry)
        return result

    enriched = await ctx.step("enrich_thread_context", _enrich, step_kind="tool_call")

    prompt = textwrap.dedent(
        f"""
        Draft one short reply for each Slack thread below.

        You have the original thread messages and the PR summary for context.
        Write like a coworker dropping a quick follow-up.

        Tell the user what changed and that it's live. Include the PR link.
        If the user should do something differently next time to get better
        results (e.g. use a flag, try a different phrasing, use a new skill),
        mention that briefly too.

        Critical style rules:
        - Each reply MUST sound different from the others. Vary the opening,
          the structure, the length, the energy. Do not start every reply
          with "I noticed".
        - Be concise. 1 to 2 sentences is ideal. 3 max.
        - Be genuinely friendly and natural — not corporate-friendly.
        - If the fix is genuinely cool or impressive, be upbeat about it.
          "This one's pretty nice" or "really happy with how this turned out"
          is fine when earned.
        - If it's a small fix, keep it low-key. Don't hype a one-liner.
        - No internal jargon. No "gap analysis", "self-improvement loop",
          "we identified", "we ran analysis".
        - Match the energy of the original thread. If the user was casual,
          be casual back. If they were all-business, be direct.

        Examples of GOOD variety (notice how different each one sounds):
        - "Fixed this — the bot now checks all your asks before sending the
          final answer, so it won't drop the last thing you said. Live now:
          [link]"
        - "This should be way faster now. Put a cap on how deep it digs for
          bounded research asks so it stops over-searching. [link]"
        - "Heads up, event threads like this now route to the events persona
          automatically. You can also use --events to force it. [link]"
        - "Pretty happy with this one — built a proper people-research skill
          so bio/rerank tasks like this are way less ad-hoc now. Next time
          you can ask for a 'people landscape' and it'll use the new skill
          directly. [link]"

        Return JSON only in this exact shape:
        {{
          "replies": [
            {{"pr_number": 1, "thread_key": "C123:1700.100", "message": "Short reply"}}
          ]
        }}

        Notifications with thread context:
        ```json
        {json.dumps(enriched, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()

    async def _draft() -> dict[str, Any]:
        result = await ctx.agent_turn(
            prompt,
            thread_key=f"workflow:{ctx.run_id}:draft-replies",
            delivery=Delivery.dev(),
            prompt_selector="eng",
            metadata={
                "source": WORKFLOW_NAME,
                "stage": "draft_thread_replies",
            },
        )
        return extract_json_payload(
            str(result.get("result_text") or ""),
            preferred_keys=("replies",),
        )

    drafted = await ctx.step("draft_thread_replies", _draft, step_kind="review")
    if "replies" not in drafted:
        ctx.log(
            "self_improve_notifier_draft_parse_mismatch",
            payload_keys=sorted(drafted.keys()),
        )
    return _normalize_reply_drafts(drafted)


async def _humanize_thread_replies(
    ctx: WorkflowContext,
    *,
    notifications: list[dict[str, Any]],
    drafted_replies: dict[tuple[int, str], str],
) -> dict[tuple[int, str], str]:
    if not drafted_replies:
        return {}

    payload = []
    for entry in notifications:
        pr_number = int(entry["pr_number"])
        thread_key = str(entry["thread_key"])
        message = drafted_replies.get((pr_number, thread_key))
        if not message:
            continue
        payload.append(
            {
                "pr_number": pr_number,
                "thread_key": thread_key,
                "message": message,
            }
        )
    if not payload:
        return {}

    prompt = textwrap.dedent(
        f"""
        Load the `humanizer` skill first.

        Polish each draft Slack reply so it reads like a real person wrote it.

        These replies go back into the original user thread after an improvement
        ships. The draft already has the right substance — your job is to make
        each one sound natural, unique, and human.

        Rules:
        - 1 to 3 short sentences
        - keep the PR URL
        - keep the substance of what changed
        - make each reply sound DIFFERENT from the others — vary the opening
          word, sentence count, punctuation, energy level
        - kill any repetitive patterns across replies (same opener, same
          structure, same "I noticed X so I did Y" formula)
        - remove AI vocabulary, hedging, filler, and corporate-friendly tone
        - if the fix is cool, let some genuine enthusiasm through
        - if it's minor, keep it chill and short

        Return JSON only in this exact shape:
        {{
          "replies": [
            {{"pr_number": 1, "thread_key": "C123:1700.100", "message": "Polished reply"}}
          ]
        }}

        Drafts:
        ```json
        {json.dumps(payload, indent=2, ensure_ascii=False)}
        ```
        """
    ).strip()

    async def _humanize() -> dict[str, Any]:
        result = await ctx.agent_turn(
            prompt,
            thread_key=f"workflow:{ctx.run_id}:humanize-replies",
            delivery=Delivery.dev(),
            prompt_selector="eng",
            metadata={
                "source": WORKFLOW_NAME,
                "stage": "humanize_thread_replies",
            },
        )
        return extract_json_payload(
            str(result.get("result_text") or ""),
            preferred_keys=("replies",),
        )

    humanized = await ctx.step("humanize_thread_replies", _humanize, step_kind="review")
    if "replies" not in humanized:
        ctx.log(
            "self_improve_notifier_humanize_parse_mismatch",
            payload_keys=sorted(humanized.keys()),
        )
    return _normalize_reply_drafts(humanized)


async def _post_thread_reply(
    ctx: WorkflowContext,
    *,
    step_name: str,
    pr_number: int,
    thread_key: str,
    channel: str,
    thread_ts: str,
    text: str,
) -> dict[str, Any]:
    async def _send() -> dict[str, Any]:
        from api.app import get_tool_manager

        tm = get_tool_manager()
        raw = await tm.call_tool(
            "slack",
            "send_message",
            {
                "channel": channel,
                "thread_ts": thread_ts,
                "text": text,
                "no_attribution": True,
            },
        )
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, ValueError):
            payload = {"raw": raw}
        if not isinstance(payload, dict):
            payload = {"raw": payload}
        payload["pr_number"] = pr_number
        payload["thread_key"] = thread_key
        return payload

    return await ctx.step(step_name, _send, step_kind="slack_post")


async def handler(inp: Input, ctx: WorkflowContext) -> dict[str, Any]:
    if inp.status.strip().lower() != "success":
        raise ControlPlaneError(
            "INVALID_WORKFLOW_INPUT",
            "self_improve_deploy_notifier only runs after successful deploy",
            422,
        )
    if not inp.after_sha.strip():
        raise ControlPlaneError(
            "INVALID_WORKFLOW_INPUT",
            "self_improve_deploy_notifier requires after_sha",
            422,
        )

    deduped = _dedupe_notifications(list(inp.notifications or []))
    merged_pr_numbers = {
        _safe_int(entry.get("pr_number"))
        for entry in list(inp.merged_prs or [])
        if _safe_int(entry.get("pr_number"))
    }
    if not merged_pr_numbers:
        merged_pr_numbers = {int(entry["pr_number"]) for entry in deduped}

    ctx.log(
        "self_improve_notifier_started",
        merged_pr_count=len(merged_pr_numbers),
        notification_count=len(deduped),
    )
    if len(deduped) < len(list(inp.notifications or [])):
        ctx.log(
            "self_improve_notifier_deduped",
            original_count=len(list(inp.notifications or [])),
            deduped_count=len(deduped),
        )

    already_notified = await _load_recent_notified_pairs(ctx)
    pending_notifications = _filter_already_notified(
        deduped,
        already_notified=already_notified,
    )
    if len(pending_notifications) < len(deduped):
        ctx.log(
            "self_improve_notifier_already_sent_filtered",
            original_count=len(deduped),
            pending_count=len(pending_notifications),
        )

    drafted_replies = await _draft_thread_replies(ctx, notifications=pending_notifications)
    humanized_replies = await _humanize_thread_replies(
        ctx,
        notifications=pending_notifications,
        drafted_replies=drafted_replies,
    )

    sent = 0
    notified_pairs: list[dict[str, Any]] = []
    for entry in pending_notifications:
        pr_number = int(entry["pr_number"])
        thread_key = str(entry["thread_key"])
        message = humanized_replies.get(
            (pr_number, thread_key),
            drafted_replies.get((pr_number, thread_key), _build_default_reply(entry)),
        )
        if (pr_number, thread_key) not in humanized_replies and (
            pr_number,
            thread_key,
        ) not in drafted_replies:
            ctx.log(
                "self_improve_notifier_reply_fallback",
                pr_number=pr_number,
                thread_key=thread_key,
            )
        await _post_thread_reply(
            ctx,
            step_name=_reply_step_name(pr_number, thread_key),
            pr_number=pr_number,
            thread_key=thread_key,
            channel=str(entry["channel"]),
            thread_ts=str(entry["thread_ts"]),
            text=message,
        )
        sent += 1
        notified_pairs.append({
            "pr_number": pr_number,
            "thread_key": thread_key,
        })
        ctx.log(
            "self_improve_notifier_thread_replied",
            pr_number=pr_number,
            thread_key=thread_key,
        )

    return {
        "before_sha": inp.before_sha,
        "after_sha": inp.after_sha,
        "baseline_sha": inp.baseline_sha,
        "merged_prs": len(merged_pr_numbers),
        "deployed_prs": len(merged_pr_numbers),
        "source_threads_notified": sent,
        "notified_pairs": notified_pairs,
    }
