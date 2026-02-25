from __future__ import annotations

from typing import Annotated

import asyncpg
from fastapi import APIRouter, Depends

from api.deps import get_pool, verify_api_key

router = APIRouter()


@router.get("/health")
async def health(pool: Annotated[asyncpg.Pool, Depends(get_pool)]) -> dict:
    """Unauthenticated liveness check — no sensitive data."""
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ok"}
    except Exception:
        return {"status": "degraded"}


@router.get("/health/detail", dependencies=[Depends(verify_api_key)])
async def health_detail(pool: Annotated[asyncpg.Pool, Depends(get_pool)]) -> dict:
    """Authenticated health check with sync run details."""
    db_ok = False
    last_syncs: list[dict] = []
    try:
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
            db_ok = True
            rows = await conn.fetch(
                """
                SELECT source, status, started_at, finished_at, records_synced
                FROM sync_runs
                WHERE (source, started_at) IN (
                    SELECT source, MAX(started_at) FROM sync_runs GROUP BY source
                )
                ORDER BY source
                """
            )
            last_syncs = [
                {
                    "source": r["source"],
                    "status": r["status"],
                    "started_at": r["started_at"].isoformat() if r["started_at"] else None,
                    "finished_at": r["finished_at"].isoformat() if r["finished_at"] else None,
                    "records_synced": r["records_synced"],
                }
                for r in rows
            ]
    except Exception:
        pass

    return {
        "status": "ok" if db_ok else "degraded",
        "database": db_ok,
        "last_syncs": last_syncs,
    }
