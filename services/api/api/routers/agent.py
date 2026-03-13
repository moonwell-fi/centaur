"""Agent router — execute/stop/status/reconnect."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from typing import Any

from pydantic import BaseModel

from api.agent import get_or_spawn, get_status, stop_session, stream_exec, stream_reconnect
from api.deps import require_scope, verify_api_key
from api.warm_pool import pool_status
from api.warm_pool import replenish as replenish_pool

SSE_KEEPALIVE_INTERVAL = 30  # seconds


async def _sse_with_keepalive(source: AsyncIterator[str]) -> AsyncIterator[str]:
    """Wrap an SSE source with periodic keepalive comments.

    Sends ``: keepalive\\n\\n`` every SSE_KEEPALIVE_INTERVAL seconds when the
    underlying source is silent. This prevents proxies and HTTP clients from
    treating the connection as dead during long-running tool calls (e.g. oracle).
    """
    aiter = source.__aiter__()
    while True:
        try:
            line = await asyncio.wait_for(
                aiter.__anext__(), timeout=SSE_KEEPALIVE_INTERVAL
            )
            yield f"data: {line}\n\n"
        except asyncio.TimeoutError:
            yield ": keepalive\n\n"
        except StopAsyncIteration:
            break
    yield "data: [DONE]\n\n"


router = APIRouter(
    prefix="/agent",
    tags=["agent"],
    dependencies=[Depends(verify_api_key)],
)


class ExecuteRequest(BaseModel):
    thread_key: str
    message: str | list[Any] = ""
    harness: str = "amp"
    engine: str | None = None
    platform: str | None = None
    user_id: str | None = None


@router.post("/execute", dependencies=[Depends(require_scope("agent:execute"))])
async def execute(req: ExecuteRequest):
    session = await get_or_spawn(req.thread_key, req.harness, engine=req.engine)

    return StreamingResponse(
        _sse_with_keepalive(
            stream_exec(
                session,
                req.message,
                platform=req.platform,
                user_id=req.user_id,
            )
        ),
        media_type="text/event-stream",
    )


class ReconnectRequest(BaseModel):
    thread_key: str
    harness: str = "amp"
    engine: str | None = None
    skip_done_count: int = 0


@router.post("/reconnect", dependencies=[Depends(require_scope("agent:execute"))])
async def reconnect(req: ReconnectRequest):
    """Re-attach to a running container's stdout without sending a new turn.

    Used by the slackbot to recover an in-progress stream after an API restart.
    Returns 404 if no running session exists for this thread.
    """
    session = await get_or_spawn(req.thread_key, req.harness, engine=req.engine)

    return StreamingResponse(
        _sse_with_keepalive(
            stream_reconnect(session, skip_done_count=req.skip_done_count)
        ),
        media_type="text/event-stream",
    )


class StopRequest(BaseModel):
    thread_key: str


@router.post("/stop", dependencies=[Depends(require_scope("agent:stop"))])
async def stop(req: StopRequest):
    ok = await stop_session(req.thread_key)
    return {"ok": ok}


@router.get("/status", dependencies=[Depends(require_scope("agent:status"))])
async def status(key: str):
    return await get_status(key)


@router.get("/pool", dependencies=[Depends(require_scope("admin"))])
async def pool():
    """Return warm pool diagnostics."""
    return pool_status()


@router.post("/pool/replenish", dependencies=[Depends(require_scope("admin"))])
async def pool_replenish():
    """Manually trigger pool replenishment."""
    spawned = await replenish_pool()
    return {"spawned": spawned, **pool_status()}


