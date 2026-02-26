"""Thread viewer API — reads agent sessions and turns from Postgres."""

from __future__ import annotations

import json
from typing import Annotated, Any

import asyncpg
from fastapi import APIRouter, Depends, HTTPException

from api.deps import get_pool, verify_api_key

router = APIRouter(
    prefix="/threads",
    tags=["threads"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("")
async def list_threads(
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict[str, Any]:
    """List all agent sessions with summary info."""
    rows = await pool.fetch(
        """
        SELECT
            s.slack_thread_key,
            s.container_id,
            s.harness,
            s.agent_thread_id,
            s.state,
            extract(epoch from s.created_at)    AS created_at,
            extract(epoch from s.last_activity) AS last_activity,
            coalesce(tc.turn_count, 0)          AS turn_count,
            coalesce(lt.result, '')              AS last_result
        FROM agent_sessions s
        LEFT JOIN LATERAL (
            SELECT count(*) AS turn_count
            FROM agent_turns t WHERE t.slack_thread_key = s.slack_thread_key
        ) tc ON true
        LEFT JOIN LATERAL (
            SELECT t.result
            FROM agent_turns t
            WHERE t.slack_thread_key = s.slack_thread_key
            ORDER BY t.turn_id DESC LIMIT 1
        ) lt ON true
        ORDER BY s.last_activity DESC
        """
    )
    threads = []
    for r in rows:
        threads.append(
            {
                "slack_thread_key": r["slack_thread_key"],
                "container_id": r["container_id"][:12],
                "harness": r["harness"],
                "agent_thread_id": r["agent_thread_id"],
                "state": r["state"],
                "created_at": float(r["created_at"]),
                "last_activity": float(r["last_activity"]),
                "turn_count": r["turn_count"],
                "last_result": (r["last_result"] or "")[:200],
            }
        )
    return {"threads": threads, "count": len(threads)}


@router.get("/detail")
async def get_thread(
    key: str,
    pool: Annotated[asyncpg.Pool, Depends(get_pool)],
) -> dict[str, Any]:
    """Get full event stream for a specific thread."""
    session = await pool.fetchrow(
        """
        SELECT
            slack_thread_key,
            container_id,
            harness,
            agent_thread_id,
            state,
            extract(epoch from created_at)    AS created_at,
            extract(epoch from last_activity) AS last_activity
        FROM agent_sessions
        WHERE slack_thread_key = $1
        """,
        key,
    )
    if not session:
        raise HTTPException(status_code=404, detail=f"Thread '{key}' not found")

    turn_rows = await pool.fetch(
        """
        SELECT
            turn_id,
            user_message,
            events,
            result,
            extract(epoch from started_at)  AS started_at,
            extract(epoch from finished_at) AS finished_at,
            exit_code,
            timed_out,
            duration_s
        FROM agent_turns
        WHERE slack_thread_key = $1
        ORDER BY turn_id
        """,
        key,
    )

    turns = []
    for t in turn_rows:
        events_raw = t["events"]
        if isinstance(events_raw, str):
            events_raw = json.loads(events_raw)
        turns.append(
            {
                "turn_id": t["turn_id"],
                "user_message": t["user_message"],
                "events": events_raw,
                "result": t["result"],
                "started_at": float(t["started_at"]) if t["started_at"] else None,
                "finished_at": float(t["finished_at"]) if t["finished_at"] else None,
                "exit_code": t["exit_code"],
                "timed_out": t["timed_out"],
                "duration_s": float(t["duration_s"]),
            }
        )

    return {
        "slack_thread_key": session["slack_thread_key"],
        "container_id": session["container_id"][:12],
        "harness": session["harness"],
        "agent_thread_id": session["agent_thread_id"],
        "state": session["state"],
        "created_at": float(session["created_at"]),
        "last_activity": float(session["last_activity"]),
        "turns": turns,
    }
