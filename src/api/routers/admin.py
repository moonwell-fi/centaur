from __future__ import annotations

import structlog
from fastapi import APIRouter, Depends, Request
from starlette.concurrency import run_in_threadpool

from api.deps import verify_api_key

log = structlog.get_logger()

router = APIRouter(prefix="/admin", dependencies=[Depends(verify_api_key)])


@router.post("/reload-tools")
async def reload_tools(request: Request) -> dict:
    """Hot-reload all tools without restarting the API server."""
    tool_manager = request.app.state.tool_manager
    result = await run_in_threadpool(tool_manager.reload)
    log.info("tools_reloaded", **result)
    return result
