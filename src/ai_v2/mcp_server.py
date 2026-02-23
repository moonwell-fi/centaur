from __future__ import annotations

import json
import re
from typing import Any

import asyncpg
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from .cli_tools import ALLOWED_CLIS, run_cli
from .deps import EmbeddingService

mcp = FastMCP(
    "Tempo AI v2",
    streamable_http_path="/",
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)  # mounted at /mcp in app.py

_pool: asyncpg.Pool | None = None

DISALLOWED_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE)\b",
    re.IGNORECASE,
)


def set_pool(pool: asyncpg.Pool) -> None:
    global _pool
    _pool = pool


def _get_pool() -> asyncpg.Pool:
    if _pool is None:
        raise RuntimeError("Database pool not initialized")
    return _pool


def _serialize(rows: list[asyncpg.Record]) -> str:
    return json.dumps([dict(r) for r in rows], default=str)


@mcp.tool()
async def search(query: str, sources: list[str] | None = None, limit: int = 20) -> str:
    """Hybrid semantic + keyword search across all ingested data."""
    pool = _get_pool()
    svc = EmbeddingService(pool=pool)
    embedding = await svc.embed(query)
    embedding_literal = "[" + ",".join(str(v) for v in embedding) + "]"

    source_filter = ""
    args: list[Any] = [query, limit]
    if sources:
        placeholders = ",".join(f"${i + 3}" for i in range(len(sources)))
        source_filter = f"AND source IN ({placeholders})"
        args.extend(sources)

    sql = f"""
    WITH vector_results AS (
        SELECT id, source, kind, source_id, content, metadata,
               ROW_NUMBER() OVER (
                   ORDER BY embedding <=> '{embedding_literal}'::vector
               ) AS vector_rank
        FROM embeddings
        WHERE TRUE {source_filter}
        ORDER BY embedding <=> '{embedding_literal}'::vector
        LIMIT $2
    ),
    fts_results AS (
        SELECT id, source, kind, source_id, content, metadata,
               ROW_NUMBER() OVER (
                   ORDER BY ts_rank_cd(tsv, plainto_tsquery('english', $1)) DESC
               ) AS fts_rank
        FROM embeddings
        WHERE tsv @@ plainto_tsquery('english', $1) {source_filter}
        LIMIT $2
    ),
    combined AS (
        SELECT
            COALESCE(v.id, f.id) AS id,
            COALESCE(v.source, f.source) AS source,
            COALESCE(v.kind, f.kind) AS kind,
            COALESCE(v.source_id, f.source_id) AS source_id,
            COALESCE(v.content, f.content) AS content,
            COALESCE(v.metadata, f.metadata) AS metadata,
            COALESCE(1.0 / (60 + v.vector_rank), 0) +
            COALESCE(1.0 / (60 + f.fts_rank), 0) AS rrf_score
        FROM vector_results v
        FULL OUTER JOIN fts_results f ON v.id = f.id
    )
    SELECT source, kind, source_id, content, metadata, rrf_score AS score
    FROM combined
    ORDER BY rrf_score DESC
    LIMIT $2
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return _serialize(rows)


@mcp.tool()
async def sql_query(query: str) -> str:
    """Run a read-only SQL query against raw_records (JSONB) or embeddings."""
    if DISALLOWED_SQL.search(query):
        return json.dumps({"error": "Only read-only queries are allowed"})

    pool = _get_pool()
    async with pool.acquire() as conn:
        try:
            rows = await conn.fetch(query)
        except Exception as e:
            return json.dumps({"error": str(e)})
    return _serialize(rows)


@mcp.tool()
async def get_slack_thread(channel: str, thread_ts: str) -> str:
    """Fetch a full Slack thread by channel and thread timestamp."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (external_id)
                external_id,
                data->>'user' AS user_id,
                data->>'text' AS text,
                data->>'ts' AS slack_ts,
                fetched_at
            FROM raw_records
            WHERE source = 'slack' AND kind = 'message'
              AND data->>'channel' = $1
              AND data->>'thread_ts' = $2
            ORDER BY external_id, fetched_at DESC
            """,
            channel,
            thread_ts,
        )
    return _serialize(rows)


@mcp.tool()
async def get_person(slug: str) -> str:
    """Get a person's profile and cross-source identities."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        person = await conn.fetchrow(
            "SELECT slug, name, email, role, focus_area FROM people WHERE slug = $1",
            slug,
        )
        if not person:
            return json.dumps({"error": "Person not found"})

        mappings = await conn.fetch(
            "SELECT source, external_id FROM entity_mappings WHERE person_slug = $1",
            slug,
        )

    result = {
        **dict(person),
        "identities": [dict(r) for r in mappings],
    }
    return json.dumps(result, default=str)


@mcp.tool()
async def get_timeline(days: int = 7, source: str | None = None) -> str:
    """Recent raw records across sources."""
    pool = _get_pool()
    conditions = [f"fetched_at >= NOW() - INTERVAL '{days} days'"]
    args: list[Any] = []
    idx = 1

    if source:
        conditions.append(f"source = ${idx}")
        args.append(source)
        idx += 1

    where = "WHERE " + " AND ".join(conditions)
    args.append(100)

    sql = f"""
    SELECT DISTINCT ON (source, kind, external_id)
        source, kind, external_id,
        COALESCE(data->>'title', data->>'text', data->>'name') AS title,
        COALESCE(data->>'url', data->>'html_url') AS url,
        fetched_at
    FROM raw_records
    {where}
    ORDER BY source, kind, external_id, fetched_at DESC
    LIMIT ${idx}
    """

    async with pool.acquire() as conn:
        rows = await conn.fetch(sql, *args)
    return _serialize(rows)


@mcp.tool()
async def list_sources() -> str:
    """List available data sources with record counts."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT source, COUNT(*) AS record_count,
                   COUNT(DISTINCT kind) AS kind_count
            FROM raw_records
            GROUP BY source
            ORDER BY source
            """
        )
    return _serialize(rows)


@mcp.tool()
async def sync_status() -> str:
    """Current sync status per source."""
    pool = _get_pool()
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT
                c.source,
                c.cursor AS cursor_value,
                c.updated_at AS cursor_updated_at,
                r.status AS last_run_status,
                r.started_at AS last_run_started,
                r.finished_at AS last_run_finished,
                r.records_synced AS last_run_records
            FROM sync_cursors c
            LEFT JOIN LATERAL (
                SELECT status, started_at, finished_at, records_synced
                FROM sync_runs
                WHERE source = c.source
                ORDER BY started_at DESC
                LIMIT 1
            ) r ON TRUE
            ORDER BY c.source
            """
        )
    return _serialize(rows)


# --- CLI tool wrappers ---


@mcp.tool()
async def cli(tool: str, args: list[str]) -> str:
    """Run a Paradigm CLI tool. Use list_tools() to see available tools and their descriptions.

    Examples:
        cli("slack", ["search", "reth benchmarks"])
        cli("slack", ["channel", "engineering", "-n", "20"])
        cli("slack", ["thread", "https://slack.com/archives/C01/p1234"])
        cli("reshift", ["notes", "search", "deal memo"])
        cli("reshift", ["db", "SELECT * FROM funds LIMIT 5"])
        cli("gsuite", ["gmail", "search", "term sheet"])
        cli("gsuite", ["calendar", "today"])
        cli("linear", ["issues", "--state", "In Progress"])
        cli("parchiver", ["search", "data room", "--limit", "10"])
        cli("allium", ["sql-examples"])
        cli("defillama", ["stablecoins"])
    """
    return await run_cli(tool, args)


@mcp.tool()
async def list_tools() -> str:
    """List all available CLI tools and their descriptions."""
    return json.dumps(ALLOWED_CLIS, indent=2)
