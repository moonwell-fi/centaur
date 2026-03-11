from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
from pathlib import Path

import click
import structlog
import uvicorn
from dotenv import load_dotenv
from etl.embeddings import EmbeddingService

from etl.invest_memo_corpus import (
    build_embedding_records as build_invest_memo_embedding_records,
)
from etl.invest_memo_corpus import (
    discover_memo_files,
    extract_memos,
    resolve_memo_paths,
)
from shared.cli_tables import render_text_table
from shared.config import Settings
from shared.db import close_pool, create_pool, execute, fetch
from shared.logging_config import configure_structlog
from shared.tool_manager import ToolManager
from shared.tool_sdk import _sm_read

load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")

configure_structlog()
log = structlog.get_logger()


@click.group()
def cli() -> None:
    """Paradigm AI v2 — Postgres+pgvector data plane, API, and sandbox."""


@cli.command()
@click.option(
    "--path",
    "paths",
    multiple=True,
    help="Memo file or directory path.",
)
@click.option("--source", default="invest_memo_corpus", show_default=True, help="Raw/embedding source")
@click.option("--kind", default="invest_memo_chunk", show_default=True, help="Embedding kind")
@click.option("--chunk-chars", default=1600, show_default=True, help="Target chunk size")
@click.option("--overlap-chars", default=220, show_default=True, help="Chunk overlap size")
@click.option("--batch-size", default=64, show_default=True, help="Embeddings per OpenAI batch")
@click.option("--max-files", default=3000, show_default=True, help="Maximum files to ingest")
@click.option("--dry-run", is_flag=True, help="Parse and chunk only (no DB writes)")
@click.option(
    "--lexical-only",
    is_flag=True,
    help="Skip vector embeddings and store lexical chunks only (no OPENAI_API_KEY required).",
)
def ingest_invest_memo_corpus(
    paths: tuple[str, ...],
    source: str,
    kind: str,
    chunk_chars: int,
    overlap_chars: int,
    batch_size: int,
    max_files: int,
    dry_run: bool,
    lexical_only: bool,
) -> None:
    """Parse local investment memos and ingest corpus chunks into Postgres."""
    configured_paths = list(paths)
    if not configured_paths:
        env_paths = os.getenv("INVEST_MEMO_PATHS", "")
        configured_paths = [p.strip() for p in env_paths.split(",") if p.strip()]

    resolved_paths = resolve_memo_paths(tuple(configured_paths))
    if not resolved_paths:
        click.echo(
            "Error: no memo paths provided. Pass --path or set INVEST_MEMO_PATHS.",
            err=True,
        )
        sys.exit(1)

    files = discover_memo_files(resolved_paths, max_files=max_files)
    if not files:
        click.echo("Error: no supported memo files found (.pdf, .txt, .md).", err=True)
        sys.exit(1)

    click.echo(f"Discovered {len(files)} memo files. Extracting text...")
    memos, skipped = extract_memos(files, resolved_paths)
    if not memos:
        click.echo("Error: no memo content extracted. See skipped list below.", err=True)
        for reason in skipped:
            click.echo(f"  - {reason}", err=True)
        sys.exit(1)

    embedding_records = build_invest_memo_embedding_records(
        memos=memos,
        source=source,
        kind=kind,
        chunk_chars=max(600, min(chunk_chars, 6000)),
        overlap_chars=max(0, min(overlap_chars, 1200)),
    )
    chunk_counts: dict[str, int] = {}
    for record in embedding_records:
        document_id = str(record.metadata.get("document_id") or "")
        if not document_id:
            continue
        chunk_counts[document_id] = chunk_counts.get(document_id, 0) + 1

    click.echo(
        "Prepared corpus payload: "
        f"{len(memos)} docs, {len(embedding_records)} chunks, {len(skipped)} skipped"
    )
    if skipped:
        for reason in skipped[:50]:
            click.echo(f"  skipped: {reason}")
        if len(skipped) > 50:
            click.echo(f"  ... {len(skipped) - 50} more skipped files")

    if dry_run:
        click.echo("Dry run complete. No DB writes performed.")
        return

    settings = Settings()
    openai_key = _sm_read("OPENAI_API_KEY") or ""
    if not openai_key and not lexical_only:
        click.echo("Error: OPENAI_API_KEY not set", err=True)
        sys.exit(1)
    if lexical_only:
        click.echo("Lexical-only mode enabled: storing chunks without vector embeddings.")

    async def _run() -> None:
        pool = await create_pool(settings.database_url)
        lexical_placeholder_vector = (
            "[" + ",".join(["0"] * settings.embedding_dimensions) + "]"
            if lexical_only
            else None
        )
        svc = None
        if not lexical_only:
            svc = EmbeddingService(
                openai_key,
                settings.embedding_model,
                settings.embedding_dimensions,
            )
        try:
            for memo in memos:
                await execute(
                    pool,
                    """
                    INSERT INTO raw_records (source, kind, external_id, content_hash, data)
                    VALUES ($1, $2, $3, $4, $5::jsonb)
                    ON CONFLICT (source, kind, external_id, content_hash)
                    DO UPDATE SET
                      fetched_at = now(),
                      data = EXCLUDED.data
                    """,
                    source,
                    "document",
                    memo.document_id,
                    memo.content_hash,
                    json.dumps(
                        {
                            "memo_name": memo.memo_name,
                            "relative_path": memo.relative_path,
                            "stage_hint": memo.stage_hint,
                            "type_hint": memo.type_hint,
                            "content_hash": memo.content_hash,
                            "char_count": len(memo.content),
                            "chunk_count": chunk_counts.get(memo.document_id, 0),
                        }
                    ),
                )

            stored_total = 0
            if lexical_only:
                for index in range(0, len(embedding_records), batch_size):
                    batch = embedding_records[index : index + batch_size]
                    for record in batch:
                        metadata = dict(record.metadata)
                        metadata["embedding_pending"] = True
                        await execute(
                            pool,
                            """
                            INSERT INTO embeddings (source, kind, source_id, content, embedding, metadata)
                            VALUES ($1, $2, $3, $4, $5::vector, $6::jsonb)
                            ON CONFLICT (source, kind, source_id) DO UPDATE SET
                                content = EXCLUDED.content,
                                embedding = CASE
                                    WHEN coalesce(embeddings.metadata->>'embedding_pending', 'false') = 'true'
                                        THEN EXCLUDED.embedding
                                    ELSE embeddings.embedding
                                END,
                                metadata = CASE
                                    WHEN coalesce(embeddings.metadata->>'embedding_pending', 'false') = 'true'
                                        THEN EXCLUDED.metadata
                                    ELSE EXCLUDED.metadata - 'embedding_pending'
                                END,
                                created_at = now()
                            """,
                            record.source,
                            record.kind,
                            record.source_id,
                            record.content,
                            lexical_placeholder_vector,
                            json.dumps(metadata),
                        )
                        stored_total += 1
                    click.echo(
                        f"  stored lexical batch {index // batch_size + 1}: {len(batch)} records"
                    )
            else:
                assert svc is not None
                for index in range(0, len(embedding_records), batch_size):
                    batch = embedding_records[index : index + batch_size]
                    stored = await svc.embed_and_store(pool, batch)
                    stored_total += stored
                    click.echo(
                        f"  embedded batch {index // batch_size + 1}: {stored} records"
                    )

            chunk_mode = "stored (lexical-only)" if lexical_only else "embedded"
            click.echo(
                f"Ingest complete: {len(memos)} docs written, {stored_total} chunks {chunk_mode}"
            )
        finally:
            await close_pool(pool)

    asyncio.run(_run())


@cli.command()
@click.option("--source", default="invest_memo_corpus", show_default=True, help="Embedding source")
@click.option("--kind", default="invest_memo_chunk", show_default=True, help="Embedding kind")
@click.option("--batch-size", default=64, show_default=True, help="Rows per embedding batch")
def backfill_invest_memo_embeddings(source: str, kind: str, batch_size: int) -> None:
    """Fill missing vectors for lexical-only memo chunks."""
    openai_key = _sm_read("OPENAI_API_KEY") or ""
    if not openai_key:
        click.echo("Error: OPENAI_API_KEY not set", err=True)
        sys.exit(1)

    settings = Settings()

    async def _run() -> None:
        pool = await create_pool(settings.database_url)
        svc = EmbeddingService(
            openai_key,
            settings.embedding_model,
            settings.embedding_dimensions,
        )
        try:
            rows = await fetch(
                pool,
                """
                SELECT source_id, content
                FROM embeddings
                WHERE source = $1
                  AND kind = $2
                  AND metadata->>'embedding_pending' = 'true'
                ORDER BY created_at ASC
                """,
                source,
                kind,
            )
            if not rows:
                click.echo("No lexical-only rows found; nothing to backfill.")
                return

            click.echo(f"Backfilling vectors for {len(rows)} rows...")
            total = 0
            for index in range(0, len(rows), max(1, batch_size)):
                batch = rows[index : index + max(1, batch_size)]
                texts = [str(row["content"] or "") for row in batch]
                vectors = await svc.embed_texts(texts)
                async with pool.acquire() as conn:
                    for row, vector in zip(batch, vectors, strict=True):
                        vec_str = "[" + ",".join(str(v) for v in vector) + "]"
                        await conn.execute(
                            """
                            UPDATE embeddings
                            SET embedding = $1::vector,
                                metadata = coalesce(metadata, '{}'::jsonb) - 'embedding_pending',
                                created_at = now()
                            WHERE source = $2
                              AND kind = $3
                              AND source_id = $4
                            """,
                            vec_str,
                            source,
                            kind,
                            row["source_id"],
                        )
                total += len(batch)
                click.echo(f"  backfilled batch {index // max(1, batch_size) + 1}: {len(batch)} rows")
            click.echo(f"Backfill complete: {total} rows updated")
        finally:
            await close_pool(pool)

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Tool commands
# ---------------------------------------------------------------------------


@cli.group("tools")
def tools_group() -> None:
    """Discover and test tool imports, tools, and CLIs."""


@tools_group.command("list")
def tools_list() -> None:
    """List discovered tools and tools from the tool manager."""
    app_root = Path(__file__).resolve().parent.parent.parent
    tools_dir = Path(app_root / "tools")

    manager = ToolManager(tools_dir)
    manager.discover()

    rows = []
    for entry in manager.tool_test_matrix():
        rows.append(
            {
                "tool": entry["tool"],
                "tools": str(len(entry["discovered_methods"])),
                "aliases": ", ".join(entry["aliases"]) or "-",
                "cli": "yes" if entry["cli_available"] else "no",
                "cli_path": entry["cli_path"],
            }
        )

    if not rows:
        click.echo("No tools loaded.")
        return

    headers = ["Tool", "Tools", "Aliases", "CLI", "CLI Path"]
    table_rows = [
        [row["tool"], row["tools"], row["aliases"], row["cli"], row["cli_path"]]
        for row in sorted(rows, key=lambda r: r["tool"])
    ]
    click.echo(render_text_table(headers, table_rows))


@tools_group.command("run")
@click.argument("tool")
@click.argument("args", nargs=-1, type=click.UNPROCESSED)
def tools_run(tool: str, args: tuple[str, ...]) -> None:
    """Run a tool CLI by tool name or script alias."""
    app_root = Path(__file__).resolve().parent.parent.parent
    tools_dir = Path(app_root / "tools")

    manager = ToolManager(tools_dir)
    if (tools_dir / tool).is_dir():
        manager.discover(only_names={tool})
    else:
        manager.discover()

    output = manager.run_cli(tool, list(args))
    try:
        parsed = json.loads(output)
    except json.JSONDecodeError:
        click.echo(output)
        return

    if isinstance(parsed, dict) and "error" in parsed:
        click.echo(json.dumps(parsed, indent=2), err=True)
        sys.exit(1)

    click.echo(output)


@tools_group.command("test")
@click.option(
    "--cli-args",
    default="--help",
    show_default=True,
    help="Arguments passed to each tool CLI for smoke testing.",
)
def tools_test(cli_args: str) -> None:
    """Run tool smoke tests across imports, registry, CLIs, REST routes, and schemas."""
    app_root = Path(__file__).resolve().parent.parent.parent
    tools_dir = Path(app_root / "tools")

    manager = ToolManager(tools_dir)
    manager.discover()

    registry_results = manager.smoke_test_registry()
    import_and_discovery = manager.tool_test_matrix()
    cli_results = manager.smoke_test_clis(shlex.split(cli_args))
    alias_results = manager.smoke_test_aliases(shlex.split(cli_args))
    rest_results = manager.smoke_test_rest_routes()
    schema_results = manager.smoke_test_schemas()

    failures: list[dict[str, object]] = []
    failures.extend(result for result in registry_results if result.get("status") != "ok")
    failures.extend(
        result for result in cli_results if result.get("status") not in {"ok", "missing_cli"}
    )
    failures.extend(
        result for result in alias_results if result.get("status") not in {"ok", "missing_aliases"}
    )
    failures.extend(result for result in rest_results if result.get("status") != "ok")
    failures.extend(result for result in schema_results if result.get("status") != "ok")

    click.echo(
        json.dumps(
            {
                "imports_and_discovery": import_and_discovery,
                "registry_smoke": registry_results,
                "cli_smoke": cli_results,
                "alias_smoke": alias_results,
                "rest_routes": rest_results,
                "schema_validation": schema_results,
                "summary": {
                    "tools_loaded": len(import_and_discovery),
                    "registry_failures": len(
                        [result for result in registry_results if result.get("status") != "ok"]
                    ),
                    "cli_failures": len(
                        [
                            result
                            for result in cli_results
                            if result.get("status") not in {"ok", "missing_cli"}
                        ]
                    ),
                    "alias_failures": len(
                        [
                            result
                            for result in alias_results
                            if result.get("status") not in {"ok", "missing_aliases"}
                        ]
                    ),
                    "rest_failures": len(
                        [result for result in rest_results if result.get("status") != "ok"]
                    ),
                    "schema_failures": len(
                        [result for result in schema_results if result.get("status") != "ok"]
                    ),
                },
            },
            indent=2,
        )
    )

    if failures:
        sys.exit(1)


# ---------------------------------------------------------------------------
# API command
# ---------------------------------------------------------------------------


@cli.command()
@click.option("--host", default="0.0.0.0", help="Bind host")
@click.option("--port", default=8000, type=int, help="Bind port")
@click.option("--reload", is_flag=True, help="Enable auto-reload")
def serve(host: str, port: int, reload: bool) -> None:
    """Run the API server."""
    uvicorn.run("api.app:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    cli()
