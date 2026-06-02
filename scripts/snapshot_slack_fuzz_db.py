#!/usr/bin/env python3
"""Export current DB state for local Slack fuzz case threads."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_CORPUS_DIR = Path("local-corpus/slackbot-fuzz")
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:32768/centaur"


def main() -> int:
    args = parse_args()
    corpus_dir = Path(args.corpus_dir)
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    cases = discover_slack_cases(corpus_dir)
    snapshots = [snapshot_case(args.database_url, case) for case in cases]
    output = {
        "schema": "centaur.slackbot_fuzz_db_snapshot.v1",
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "database_url_redacted": redact_database_url(args.database_url),
        "case_count": len(cases),
        "counts": {
            "sessions_missing": sum(1 for item in snapshots if item["db"]["session"] is None),
            "running_executions": sum(
                1
                for item in snapshots
                for execution in item["db"]["executions"]
                if execution.get("status") == "running"
            ),
            "threads_without_events": sum(
                1 for item in snapshots if int(item["db"]["event_range"].get("event_count") or 0) == 0
            ),
            "threads_without_terminal_events": sum(
                1 for item in snapshots if not item["db"]["terminal_events"]
            ),
        },
        "cases": snapshots,
    }
    output_path = corpus_dir / f"db-snapshot-{run_id}.json"
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(output_path)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus-dir", default=str(DEFAULT_CORPUS_DIR))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--run-id")
    return parser.parse_args()


def discover_slack_cases(corpus_dir: Path) -> list[dict[str, Any]]:
    cases = []
    seen: set[str] = set()
    for path in sorted(corpus_dir.glob("**/case.json")):
        try:
            case = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            continue
        thread_key = str(case.get("thread_key") or "")
        if not thread_key.startswith("slack:") or thread_key in seen:
            continue
        seen.add(thread_key)
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


def snapshot_case(database_url: str, case: dict[str, Any]) -> dict[str, Any]:
    thread_key = case["thread_key"]
    query = f"""
select json_build_object(
  'session', (
    select row_to_json(s)
    from sessions s
    where s.thread_key = {sql_literal(thread_key)}
  ),
  'executions', coalesce((
    select json_agg(row_to_json(e) order by e.created_at, e.execution_id)
    from (
      select execution_id, thread_key, status, metadata, error, created_at, updated_at,
             started_at, completed_at
      from session_executions
      where thread_key = {sql_literal(thread_key)}
      order by created_at, execution_id
    ) e
  ), '[]'::json),
  'event_counts', coalesce((
    select json_object_agg(event_type, count)
    from (
      select event_type, count(*)::int as count
      from session_events
      where thread_key = {sql_literal(thread_key)}
      group by event_type
      order by event_type
    ) counts
  ), '{{}}'::json),
  'event_range', (
    select json_build_object(
      'event_count', count(*)::int,
      'first_event_id', min(event_id),
      'last_event_id', max(event_id),
      'first_created_at', min(created_at),
      'last_created_at', max(created_at)
    )
    from session_events
    where thread_key = {sql_literal(thread_key)}
  ),
  'terminal_events', coalesce((
    select json_agg(row_to_json(t) order by t.event_id)
    from (
      select event_id, event_type, execution_id, created_at, payload
      from session_events
      where thread_key = {sql_literal(thread_key)}
        and (
          event_type in (
            'session.execution_completed',
            'session.execution_failed',
            'session.execution_cancelled',
            'session.stream_error',
            'session.stdout_pump_failed'
          )
          or (
            event_type = 'session.output.line'
            and (
              payload::text like '%turn.done%'
              or payload::text like '%turn.completed%'
              or payload::text like '%turn.failed%'
              or payload::text like '%\"type\": \"result\"%'
              or payload::text like '%\"type\":\"result\"%'
            )
          )
        )
      order by event_id
    ) t
  ), '[]'::json)
)::text;
"""
    return {
        **case,
        "db": run_json_query(database_url, query),
    }


def run_json_query(database_url: str, query: str) -> dict[str, Any]:
    env = os.environ.copy()
    env.setdefault("PGPASSWORD", database_password(database_url))
    result = subprocess.run(
        ["psql", database_dsn_for_psql(database_url), "-At", "-c", query],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    return json.loads(result.stdout.strip() or "{}")


def sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def database_password(database_url: str) -> str:
    parsed = urllib.parse.urlparse(database_url)
    return urllib.parse.unquote(parsed.password or "postgres")


def database_dsn_for_psql(database_url: str) -> str:
    parsed = urllib.parse.urlparse(database_url)
    username = urllib.parse.unquote(parsed.username or "postgres")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or 5432
    db = parsed.path.lstrip("/") or "centaur"
    return f"postgresql://{username}@{host}:{port}/{db}"


def redact_database_url(database_url: str) -> str:
    parsed = urllib.parse.urlparse(database_url)
    netloc = parsed.hostname or ""
    if parsed.port:
        netloc += f":{parsed.port}"
    return urllib.parse.urlunparse(
        (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
    )


if __name__ == "__main__":
    raise SystemExit(main())
