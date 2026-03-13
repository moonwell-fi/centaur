"""Who Said It? game — Slack-native multiplayer quote guessing."""

from __future__ import annotations

import asyncio
import os
import random
import re
import time
from typing import Any

import httpx
import structlog
from fastapi import APIRouter, Depends, HTTPException, Request

from api.deps import require_scope, verify_api_key
from api.game_state import (
    RoundData,
    game_store,
)

log = structlog.get_logger().bind(service="api", component="game")

router = APIRouter(
    prefix="/game",
    tags=["game"],
    dependencies=[Depends(verify_api_key), Depends(require_scope("game"))],
)

ROUND_COUNT = 10
ROUND_SECONDS = 15
OPTIONS_COUNT = 5
QUOTE_MIN_LEN = 30
MESSAGES_TO_FETCH = 500

SLACK_API = "https://slack.com/api"


def _strip_mentions(text: str) -> str:
    """Remove <@USER_ID> and <@USER_ID|name> from text."""
    return re.sub(r"<@[A-Z0-9]+(?:\|[^>]+)?>", "", text).strip()


def _get_slack_token() -> str:
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    if not token:
        raise RuntimeError("SLACK_BOT_TOKEN not set")
    return token


async def _slack_get(
    client: httpx.AsyncClient,
    method: str,
    params: dict[str, str] | None = None,
) -> dict[str, Any]:
    token = _get_slack_token()
    resp = await client.get(
        f"{SLACK_API}/{method}",
        params=params or {},
        headers={"Authorization": f"Bearer {token}"},
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
    return data


async def _slack_post(
    client: httpx.AsyncClient,
    method: str,
    json: dict[str, Any] | None = None,
) -> dict[str, Any]:
    token = _get_slack_token()
    resp = await client.post(
        f"{SLACK_API}/{method}",
        json=json or {},
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        timeout=30,
    )
    data = resp.json()
    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error', 'unknown')}")
    return data


async def _fetch_quotes(channel_id: str) -> list[dict[str, Any]]:
    """Fetch messages from channel, filter to usable quotes."""
    async with httpx.AsyncClient() as client:
        cursor = None
        all_messages: list[dict[str, Any]] = []
        while len(all_messages) < MESSAGES_TO_FETCH:
            params: dict[str, Any] = {
                "channel": channel_id,
                "limit": min(200, MESSAGES_TO_FETCH - len(all_messages)),
            }
            if cursor:
                params["cursor"] = cursor
            data = await _slack_get(client, "conversations.history", params)
            msgs = data.get("messages", [])
            if not msgs:
                break
            for m in msgs:
                if m.get("bot_id") or m.get("subtype"):
                    continue
                user_id = m.get("user", "")
                if not user_id:
                    continue
                text = m.get("text", "")
                if not text or len(text) < QUOTE_MIN_LEN:
                    continue
                if text.strip().startswith(("<", ":")):
                    continue
                all_messages.append({
                    "user_id": user_id,
                    "text": text,
                    "ts": m.get("ts", ""),
                })
            cursor = data.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        users_data = await _slack_get(client, "users.list", {"limit": "1000"})
        user_map: dict[str, str] = {}
        for u in users_data.get("members", []):
            if u.get("deleted") or u.get("is_bot"):
                continue
            user_map[u["id"]] = u.get("real_name") or u.get("name", u["id"])

        result = []
        seen_authors: set[str] = set()
        for m in all_messages:
            uid = m["user_id"]
            if uid in seen_authors:
                continue
            name = user_map.get(uid, uid)
            text = _strip_mentions(m["text"])
            if len(text) < QUOTE_MIN_LEN:
                continue
            result.append({
                "user_id": uid,
                "user_name": name,
                "text": text,
                "ts": m["ts"],
            })
            seen_authors.add(uid)

        return result


def _build_rounds(quotes: list[dict[str, Any]]) -> list[RoundData]:
    """Build 10 rounds with 5 options each."""
    if len(quotes) < ROUND_COUNT:
        raise ValueError(
            f"Need at least {ROUND_COUNT} unique authors, found {len(quotes)}"
        )
    chosen = random.sample(quotes, ROUND_COUNT)
    all_authors = {(q["user_id"], q["user_name"]) for q in quotes}
    rounds = []
    for q in chosen:
        correct = (q["user_id"], q["user_name"])
        others = list(all_authors - {correct})
        if len(others) < OPTIONS_COUNT - 1:
            others = list(all_authors)
        decoys = random.sample(others, OPTIONS_COUNT - 1)
        options = [{"user_id": correct[0], "name": correct[1]}] + [
            {"user_id": d[0], "name": d[1]} for d in decoys
        ]
        random.shuffle(options)
        rounds.append(
            RoundData(
                quote_id=q["ts"],
                quote_text=q["text"],
                correct_user_id=q["user_id"],
                correct_user_name=q["user_name"],
                options=options,
            )
        )
    return rounds


def _blocks_round(
    game_id: str,
    round_data: RoundData,
    round_num: int,
    seconds_left: int,
) -> list[dict[str, Any]]:
    """Block Kit blocks for a round message."""
    actions = []
    for opt in round_data.options:
        actions.append({
            "type": "button",
            "text": {"type": "plain_text", "text": opt["name"], "emoji": True},
            "action_id": "whosaidit_pick",
            "value": f"{game_id}|{opt['user_id']}",
        })
    return [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "🎮 Who Said It?", "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Round {round_num} of {ROUND_COUNT}*",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f'_{round_data.quote_text}_',
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "Who said this?"}},
        {"type": "actions", "elements": actions},
        {
            "type": "context",
            "elements": [
                {"type": "mrkdwn", "text": f"⏱️ {seconds_left} seconds remaining"},
            ],
        },
    ]


def _blocks_results(
    correct_name: str,
    top_scores: list[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Block Kit blocks for round results (no buttons)."""
    lines = [f"✅ It was *{correct_name}*!", "", "🏆 *Scoreboard*"]
    for i, (name, pts) in enumerate(top_scores, 1):
        lines.append(f"{i}. {name} ········ {pts} pts")
    return [
        {"type": "section", "text": {"type": "mrkdwn", "text": "Round Results"}},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": "\n".join(lines)},
        },
    ]


def _blocks_game_over(
    top_scores: list[tuple[str, int]],
) -> list[dict[str, Any]]:
    """Block Kit blocks for game over + Play Again button."""
    lines = ["🏆 *Game Over!*", "", "Final Scores:"]
    medals = ["🥇", "🥈", "🥉"]
    for i, (name, pts) in enumerate(top_scores, 1):
        medal = medals[i - 1] if i <= 3 else f"{i}."
        lines.append(f"{medal} {name} ········ {pts} pts")
    blocks = [
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Play Again", "emoji": True},
                    "action_id": "whosaidit_play_again",
                    "value": "play_again",
                },
            ],
        },
    ]
    return blocks


async def _run_round_timer(
    game_id: str,
    channel_id: str,
    thread_ts: str,
) -> None:
    """Background task: wait ROUND_SECONDS, then reveal and post next round."""
    await asyncio.sleep(ROUND_SECONDS)
    g = game_store.get_game(game_id)
    if not g or g.state != "playing":
        return
    round_data = game_store.get_current_round(game_id)
    if not round_data:
        return
    result = game_store.finalize_round(game_id)
    if not result:
        return
    top = game_store.get_top_n(game_id, 5)
    top_tuples = [(p.name, p.points) for p in top]
    async with httpx.AsyncClient() as client:
        if g.round_message_ts:
            await _slack_post(
                client,
                "chat.update",
                {
                    "channel": channel_id,
                    "ts": g.round_message_ts,
                    "blocks": _blocks_results(
                        result["correct_user_name"],
                        top_tuples,
                    ),
                },
            )
        await asyncio.sleep(5)
    if game_store.is_finished(game_id):
        top_all = game_store.get_scores_sorted(game_id)
        top_tuples_all = [(p.name, p.points) for p in top_all]
        async with httpx.AsyncClient() as client:
            await _slack_post(
                client,
                "chat.postMessage",
                {
                    "channel": channel_id,
                    "thread_ts": thread_ts,
                    "blocks": _blocks_game_over(top_tuples_all),
                },
            )
        game_store.cleanup(game_id)
        return
    next_round = game_store.get_current_round(game_id)
    if not next_round:
        return
    async with httpx.AsyncClient() as client:
        resp = await _slack_post(
            client,
            "chat.postMessage",
            {
                "channel": channel_id,
                "thread_ts": thread_ts,
                "blocks": _blocks_round(
                    game_id,
                    next_round,
                    game_store.get_game(game_id).current_round_idx + 1,
                    ROUND_SECONDS,
                ),
            },
        )
        ts = resp.get("ts", "")
        game_store.set_round_message_ts(game_id, ts)
    asyncio.create_task(
        _run_round_timer(game_id, channel_id, thread_ts),
    )


@router.post("/start")
async def game_start(request: Request) -> dict[str, Any]:
    """Start a game. Called by slackbot when user says @centaur play whosaidit."""
    body = await request.json()
    channel_id = body.get("channel_id")
    thread_ts = body.get("thread_ts")
    if not channel_id or not thread_ts:
        raise HTTPException(400, "channel_id and thread_ts required")
    try:
        quotes = await _fetch_quotes(channel_id)
        rounds = _build_rounds(quotes)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:
        log.error("game_start_fetch_failed", error=str(e), channel=channel_id)
        raise HTTPException(500, f"Failed to fetch quotes: {e}") from e
    game_id = game_store.create_game(channel_id, thread_ts, rounds)
    round_data = rounds[0]
    async with httpx.AsyncClient() as client:
        resp = await _slack_post(
            client,
            "chat.postMessage",
            {
                "channel": channel_id,
                "thread_ts": thread_ts,
                "blocks": _blocks_round(game_id, round_data, 1, ROUND_SECONDS),
            },
        )
        ts = resp.get("ts", "")
        game_store.set_round_message_ts(game_id, ts)
    asyncio.create_task(_run_round_timer(game_id, channel_id, thread_ts))
    return {"game_id": game_id, "channel_id": channel_id, "thread_ts": thread_ts}


@router.post("/answer")
async def game_answer(request: Request) -> dict[str, Any]:
    """Record a player's answer. Called by slackbot when user clicks a button."""
    body = await request.json()
    game_id = body.get("game_id")
    user_id = body.get("user_id")
    user_name = body.get("user_name", "Someone")
    pick_user_id = body.get("pick_user_id")
    if not all([game_id, user_id, pick_user_id]):
        raise HTTPException(400, "game_id, user_id, pick_user_id required")
    ok = game_store.record_answer(
        game_id,
        user_id,
        user_name,
        pick_user_id,
        time.monotonic(),
    )
    return {"recorded": ok}


@router.get("/status/{game_id}")
async def game_status(game_id: str) -> dict[str, Any]:
    """Debug: get current game state."""
    g = game_store.get_game(game_id)
    if not g:
        return {"found": False}
    return {
        "found": True,
        "state": g.state,
        "current_round": g.current_round_idx + 1,
        "scores": {uid: s.points for uid, s in g.scores.items()},
    }
