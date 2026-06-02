#!/usr/bin/env python3
"""Slackbot/API-rs fuzz harness for Slack block vs durable event mismatches."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_API_URL = "http://127.0.0.1:8080"
DEFAULT_CHANNEL = "C0APUQ8U5T9"
DEFAULT_BOT_USER = "U0B7CFP79PF"
DEFAULT_DATABASE_URL = "postgresql://postgres:postgres@127.0.0.1:32768/centaur"


@dataclass(frozen=True)
class FuzzCase:
    case_id: str
    prompt: str
    kind: str = "safe"
    followups: tuple[tuple[float, str], ...] = ()
    expected_final_contains: tuple[str, ...] = ()
    forbidden_final_contains: tuple[str, ...] = ()
    expect_max_duration_enforced: bool = False
    max_duration_ms: int = 90_000


SLACK_CASES: tuple[FuzzCase, ...] = (
    FuzzCase(
        "pong_exact",
        "Reply with exactly PONG and nothing else.",
        max_duration_ms=60_000,
    ),
    FuzzCase(
        "command_sleep_stdout",
        "Run `bash -lc 'for i in 1 2 3; do echo step-$i; sleep 1; done'`, "
        "then answer exactly FINAL_SLEEP_STDOUT_OK.",
    ),
    FuzzCase(
        "command_nonzero_final",
        "Run `bash -lc 'echo before; false'`, then answer exactly FINAL_NONZERO_HANDLED. "
        "Do not modify files.",
    ),
    FuzzCase(
        "large_stdout",
        "Run `bash -lc 'for i in $(seq 1 140); do printf \"line-%03d\\\\n\" \"$i\"; done'`, "
        "then answer exactly FINAL_LARGE_STDOUT_OK.",
    ),
    FuzzCase(
        "invalid_command_final",
        "Run `definitely_not_a_real_command_938475`, then answer exactly FINAL_BAD_COMMAND_OK. "
        "Do not modify files.",
    ),
    FuzzCase(
        "gh_view_readonly",
        "Run `gh repo view paradigmxyz/centaur --json nameWithOwner,isPrivate,defaultBranchRef`, "
        "then summarize the returned fields in one sentence.",
    ),
    FuzzCase(
        "markdown_final_shape",
        "Do not run commands. Return a final answer with: one short paragraph, a 3-item bullet list, "
        "and a fenced json code block containing {\"ok\":true}.",
        max_duration_ms=60_000,
    ),
    FuzzCase(
        "followup_during_stream",
        "Run `bash -lc 'echo starting; sleep 8; echo done'`, then answer exactly FINAL_FOLLOWUP_ROOT_OK.",
        followups=((1.0, "While the run is active, remember this follow-up: FINAL_FOLLOWUP_REPLY_OK."),),
        max_duration_ms=120_000,
    ),
    FuzzCase(
        "steer_during_stream",
        "Run `bash -lc 'echo steering-start; sleep 10; echo steering-end'`. "
        "After the command completes, obey the latest instruction in this Slack thread for the exact final answer.",
        followups=((2.0, "STEERING UPDATE: final answer must be exactly FINAL_STEERED_OK."),),
        expected_final_contains=("FINAL_STEERED_OK",),
        forbidden_final_contains=("FINAL_FOLLOWUP_ROOT_OK",),
        max_duration_ms=120_000,
    ),
    FuzzCase(
        "continue_during_command",
        "Run `bash -lc 'echo continue-start; sleep 12; echo continue-end'`, then answer exactly "
        "FINAL_CONTINUE_ROOT_OK. Do not modify files.",
        followups=((2.0, "--claude continue"),),
        max_duration_ms=150_000,
    ),
    FuzzCase(
        "long_running_still_working",
        "Run `bash -lc 'echo long-start; sleep 18; echo long-end'`, then answer exactly "
        "FINAL_LONG_RUNNING_OK. Do not cancel unless explicitly instructed to cancel.",
        followups=((5.0, "Are you still working or did this break? Please finalize when the command is done."),),
        expected_final_contains=("FINAL_LONG_RUNNING_OK",),
        max_duration_ms=150_000,
    ),
    FuzzCase(
        "markdown_fragment_pressure",
        "Do not run commands. Return a final answer with exactly this shape: first a sentence ending in a "
        "question mark inside bold quotes, then a markdown table with two rows, then the exact sentinel "
        "FINAL_MARKDOWN_FRAGMENT_OK on its own final line.",
        expected_final_contains=("FINAL_MARKDOWN_FRAGMENT_OK",),
        max_duration_ms=60_000,
    ),
)


API_CASES: tuple[FuzzCase, ...] = (
    FuzzCase("api_pong", "Reply with exactly PONG and nothing else.", max_duration_ms=60_000),
    FuzzCase(
        "api_sleep_stdout",
        "Run `bash -lc 'echo api-start; sleep 2; echo api-end'`, then answer exactly API_SLEEP_OK.",
    ),
    FuzzCase(
        "api_nonzero",
        "Run `bash -lc 'echo api-before; false'`, then answer exactly API_NONZERO_OK.",
    ),
    FuzzCase(
        "api_large_stdout",
        "Run `bash -lc 'for i in $(seq 1 80); do echo api-line-$i; done'`, "
        "then answer exactly API_LARGE_STDOUT_OK.",
    ),
    FuzzCase(
        "api_max_duration_ignored",
        "Run `bash -lc 'echo timeout-start; sleep 12; echo timeout-end'`, "
        "then answer exactly API_TIMEOUT_SHOULD_NOT_FINISH.",
        expect_max_duration_enforced=True,
        max_duration_ms=3_000,
    ),
)


@dataclass
class CaseResult:
    case_id: str
    surface: str
    thread_key: str
    started_at: str
    completed_at: str | None = None
    ok: bool = False
    issues: list[str] = field(default_factory=list)
    summary: dict[str, Any] = field(default_factory=dict)
    artifacts: dict[str, str] = field(default_factory=dict)
    error: str | None = None


def main() -> int:
    args = parse_args()
    run_id = args.run_id or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = Path(args.output_dir) / run_id
    out_dir.mkdir(parents=True, exist_ok=True)
    write_json(
        out_dir / "run.json",
        {
            "schema": "centaur.slackbot_fuzz_run.v1",
            "run_id": run_id,
            "started_at": datetime.now(UTC).isoformat(),
            "api_url": args.api_url,
            "channel": args.channel,
            "bot_user": args.bot_user,
            "database_url": args.database_url,
            "mode": args.mode,
        },
    )
    results = asyncio.run(run_all(args, run_id, out_dir))
    write_json(
        out_dir / "summary.json",
        {
            "schema": "centaur.slackbot_fuzz_summary.v1",
            "run_id": run_id,
            "finished_at": datetime.now(UTC).isoformat(),
            "counts": {
                "cases": len(results),
                "ok": sum(1 for result in results if result.ok),
                "with_issues": sum(1 for result in results if result.issues),
                "errors": sum(1 for result in results if result.error),
            },
            "issues": [
                {
                    "case_id": result.case_id,
                    "surface": result.surface,
                    "thread_key": result.thread_key,
                    "issues": result.issues,
                    "error": result.error,
                }
                for result in results
                if result.issues or result.error
            ],
            "results": [result.__dict__ for result in results],
        },
    )
    print(out_dir)
    return 0 if all(result.ok or result.issues for result in results) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("slack", "api", "both"), default="both")
    parser.add_argument("--api-url", default=os.environ.get("CENTAUR_API_URL", DEFAULT_API_URL))
    parser.add_argument("--database-url", default=os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL))
    parser.add_argument("--channel", default=DEFAULT_CHANNEL)
    parser.add_argument("--bot-user", default=os.environ.get("SLACK_BOT_USER_ID", DEFAULT_BOT_USER))
    parser.add_argument("--output-dir", default="local-corpus/slackbot-fuzz")
    parser.add_argument("--run-id")
    parser.add_argument("--slack-concurrency", type=int, default=4)
    parser.add_argument("--api-concurrency", type=int, default=8)
    parser.add_argument("--api-repetitions", type=int, default=3)
    parser.add_argument("--slack-repetitions", type=int, default=1)
    parser.add_argument("--poll-timeout-s", type=float, default=150.0)
    parser.add_argument("--poll-interval-s", type=float, default=2.0)
    parser.add_argument("--slack-settle-s", type=float, default=8.0)
    parser.add_argument("--case", help="Only run cases whose case_id matches this regex")
    return parser.parse_args()


async def run_all(args: argparse.Namespace, run_id: str, out_dir: Path) -> list[CaseResult]:
    tasks: list[asyncio.Task[CaseResult]] = []
    case_re = re.compile(args.case) if args.case else None
    if args.mode in ("slack", "both"):
        slack_sem = asyncio.Semaphore(max(1, args.slack_concurrency))
        for rep in range(args.slack_repetitions):
            for case in SLACK_CASES:
                if case_re and not case_re.search(case.case_id):
                    continue
                tasks.append(
                    asyncio.create_task(run_slack_case(args, run_id, out_dir, case, rep, slack_sem))
                )
    if args.mode in ("api", "both"):
        api_sem = asyncio.Semaphore(max(1, args.api_concurrency))
        for rep in range(args.api_repetitions):
            for case in API_CASES:
                if case_re and not case_re.search(case.case_id):
                    continue
                tasks.append(
                    asyncio.create_task(run_api_case(args, run_id, out_dir, case, rep, api_sem))
                )
            if not case_re or case_re.search("same_thread_execute_race"):
                tasks.append(asyncio.create_task(run_api_race_case(args, run_id, out_dir, rep, api_sem)))

    results: list[CaseResult] = []
    for task in asyncio.as_completed(tasks):
        result = await task
        results.append(result)
        print(
            json.dumps(
                {
                    "case_id": result.case_id,
                    "surface": result.surface,
                    "ok": result.ok,
                    "issues": result.issues,
                    "error": result.error,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    return results


async def run_slack_case(
    args: argparse.Namespace,
    run_id: str,
    out_dir: Path,
    case: FuzzCase,
    rep: int,
    sem: asyncio.Semaphore,
) -> CaseResult:
    async with sem:
        case_name = f"slack-{rep:02d}-{case.case_id}"
        case_dir = out_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        started_at = datetime.now(UTC).isoformat()
        result = CaseResult(
            case_id=case_name,
            surface="slack",
            thread_key="",
            started_at=started_at,
        )
        try:
            prompt = f"<@{args.bot_user}> [fuzz:{run_id}:{case_name}] {case.prompt}"
            send = await slack_send(args.channel, prompt)
            write_json(case_dir / "send.json", send)
            channel = str(send["channel"])
            ts = str(send["ts"])
            result.thread_key = f"slack:{channel}:{ts}"
            result.artifacts["send"] = str(case_dir / "send.json")

            for delay, followup in case.followups:
                await asyncio.sleep(delay)
                follow_send = await slack_send(channel, followup, thread=ts)
                write_json(case_dir / f"followup-{len(result.artifacts)}.json", follow_send)

            await wait_for_terminal_events(args, result.thread_key, case.max_duration_ms)
            await asyncio.sleep(args.slack_settle_s)
            events = await fetch_db_events(args, result.thread_key)
            write_json(case_dir / "session_events.json", events)
            result.artifacts["session_events"] = str(case_dir / "session_events.json")

            dump_path = case_dir / "slack_thread.json"
            dump = await slack_thread_dump(channel, ts, dump_path)
            result.artifacts["slack_thread"] = str(dump_path)
            result.summary = compare_case(events, dump)
            apply_case_expectations(result.summary, case)
            result.issues = list(result.summary.get("issues", []))
            result.ok = not result.issues
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.issues.append("harness_error")
            write_json(case_dir / "harness_error.json", {"error": result.error})
        result.completed_at = datetime.now(UTC).isoformat()
        write_json(case_dir / "case.json", result.__dict__)
        return result


async def run_api_case(
    args: argparse.Namespace,
    run_id: str,
    out_dir: Path,
    case: FuzzCase,
    rep: int,
    sem: asyncio.Semaphore,
) -> CaseResult:
    async with sem:
        case_name = f"api-{rep:02d}-{case.case_id}"
        case_dir = out_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        thread_key = f"fuzz:{run_id}:{case_name}"
        result = CaseResult(
            case_id=case_name,
            surface="api",
            thread_key=thread_key,
            started_at=datetime.now(UTC).isoformat(),
        )
        try:
            await api_json(args.api_url, "POST", f"/api/session/{quote_path(thread_key)}", {
                "harness_type": "codex",
                "metadata": {"source": "slackbot_fuzz", "case_id": case_name},
            })
            await api_json(args.api_url, "POST", f"/api/session/{quote_path(thread_key)}/messages", {
                "messages": [
                    {
                        "role": "user",
                        "parts": [{"type": "text", "text": case.prompt}],
                        "metadata": {"source": "slackbot_fuzz", "case_id": case_name},
                    }
                ]
            })
            input_line = json.dumps(
                {
                    "type": "user",
                    "thread_key": thread_key,
                    "trace_metadata": {"source": "slackbot_fuzz", "case_id": case_name},
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": case.prompt}],
                    },
                },
                ensure_ascii=False,
            )
            execute = await api_json(
                args.api_url,
                "POST",
                f"/api/session/{quote_path(thread_key)}/execute",
                {
                    "input_lines": [input_line],
                    "idle_timeout_ms": 1000,
                    "max_duration_ms": case.max_duration_ms,
                    "metadata": {"source": "slackbot_fuzz", "case_id": case_name},
                },
            )
            write_json(case_dir / "execute.json", execute)
            result.artifacts["execute"] = str(case_dir / "execute.json")
            await wait_for_terminal_events(args, thread_key, case.max_duration_ms)
            events = await fetch_db_events(args, thread_key)
            write_json(case_dir / "session_events.json", events)
            result.artifacts["session_events"] = str(case_dir / "session_events.json")
            result.summary = compare_case(events, None)
            apply_case_expectations(result.summary, case)
            result.issues = list(result.summary.get("issues", []))
            result.ok = not result.issues
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.issues.append("harness_error")
            write_json(case_dir / "harness_error.json", {"error": result.error})
        result.completed_at = datetime.now(UTC).isoformat()
        write_json(case_dir / "case.json", result.__dict__)
        return result


async def run_api_race_case(
    args: argparse.Namespace,
    run_id: str,
    out_dir: Path,
    rep: int,
    sem: asyncio.Semaphore,
) -> CaseResult:
    async with sem:
        case_name = f"api-{rep:02d}-same_thread_execute_race"
        case_dir = out_dir / case_name
        case_dir.mkdir(parents=True, exist_ok=True)
        thread_key = f"fuzz:{run_id}:{case_name}"
        result = CaseResult(
            case_id=case_name,
            surface="api",
            thread_key=thread_key,
            started_at=datetime.now(UTC).isoformat(),
        )
        prompt = (
            "Run `bash -lc 'echo race-start; sleep 4; echo race-end'`, "
            "then answer exactly API_RACE_OK."
        )
        try:
            await api_json(args.api_url, "POST", f"/api/session/{quote_path(thread_key)}", {
                "harness_type": "codex",
                "metadata": {"source": "slackbot_fuzz", "case_id": case_name},
            })
            input_line = json.dumps(
                {
                    "type": "user",
                    "thread_key": thread_key,
                    "trace_metadata": {"source": "slackbot_fuzz", "case_id": case_name},
                    "message": {
                        "role": "user",
                        "content": [{"type": "text", "text": prompt}],
                    },
                },
                ensure_ascii=False,
            )
            body = {
                "input_lines": [input_line],
                "idle_timeout_ms": 1000,
                "max_duration_ms": 60_000,
                "metadata": {"source": "slackbot_fuzz", "case_id": case_name},
            }
            tasks = [
                asyncio.create_task(
                    api_json(args.api_url, "POST", f"/api/session/{quote_path(thread_key)}/execute", body)
                )
                for _ in range(2)
            ]
            race_results: list[dict[str, Any]] = []
            for task in tasks:
                try:
                    race_results.append({"ok": True, "response": await task})
                except Exception as exc:  # noqa: BLE001
                    race_results.append({"ok": False, "error": str(exc)})
            write_json(case_dir / "race_execute_results.json", race_results)
            result.artifacts["race_execute_results"] = str(case_dir / "race_execute_results.json")

            await wait_for_terminal_events(args, thread_key, 60_000)
            events = await fetch_db_events(args, thread_key)
            write_json(case_dir / "session_events.json", events)
            result.artifacts["session_events"] = str(case_dir / "session_events.json")
            result.summary = compare_case(events, None)
            failed_results = [item for item in race_results if not item.get("ok")]
            if failed_results:
                result.summary.setdefault("issues", []).append("api_same_thread_execute_race_error")
            result.summary["race_results"] = race_results
            result.issues = sorted(set(result.summary.get("issues", [])))
            result.ok = not result.issues
        except Exception as exc:  # noqa: BLE001
            result.error = str(exc)
            result.issues.append("harness_error")
            write_json(case_dir / "harness_error.json", {"error": result.error})
        result.completed_at = datetime.now(UTC).isoformat()
        write_json(case_dir / "case.json", result.__dict__)
        return result


async def wait_for_terminal_events(
    args: argparse.Namespace,
    thread_key: str,
    max_duration_ms: int,
) -> None:
    timeout = max(args.poll_timeout_s, max_duration_ms / 1000 + 20)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = await fetch_db_events(args, thread_key)
        db = analyze_db_events(events)
        if db["terminal_output_count"] or db["execution_failed_count"]:
            return
        await asyncio.sleep(args.poll_interval_s)


async def api_json(api_url: str, method: str, path: str, body: dict[str, Any]) -> dict[str, Any]:
    url = urllib.parse.urljoin(api_url.rstrip("/") + "/", path.lstrip("/"))
    payload = json.dumps(body).encode("utf-8")

    def call() -> dict[str, Any]:
        request = urllib.request.Request(
            url,
            data=payload,
            method=method,
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body_text = error.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"{method} {url} -> {error.code}: {body_text}") from error

    return await asyncio.to_thread(call)


async def slack_send(channel: str, text: str, thread: str | None = None) -> dict[str, Any]:
    cmd = ["./slack", "send", channel, text, "--no-attribution", "--json", "--compact"]
    if thread:
        cmd.extend(["--thread", thread])
    output = await run_command(cmd, timeout=90)
    return json.loads(output)


async def slack_thread_dump(channel: str, ts: str, output_path: Path) -> dict[str, Any]:
    await run_command(
        ["./slack", "thread", f"{channel}:{ts}", "--compact", "--output", str(output_path)],
        timeout=180,
    )
    return json.loads(output_path.read_text(encoding="utf-8"))


async def fetch_db_events(args: argparse.Namespace, thread_key: str) -> list[dict[str, Any]]:
    sql = f"""
select coalesce(json_agg(row_to_json(t) order by event_id), '[]'::json)
from (
  select event_id, thread_key, execution_id, event_type, payload, created_at
  from session_events
  where thread_key = {sql_literal(thread_key)}
  order by event_id
) t;
"""
    env = os.environ.copy()
    env.setdefault("PGPASSWORD", database_password(args.database_url))
    cmd = [
        "psql",
        database_dsn_for_psql(args.database_url),
        "-At",
        "-c",
        sql,
    ]
    output = await run_command(cmd, env=env, timeout=60)
    return json.loads(output.strip() or "[]")


async def run_command(
    cmd: list[str],
    *,
    env: dict[str, str] | None = None,
    timeout: float,
) -> str:
    process = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        raise RuntimeError(f"command timed out: {' '.join(cmd)}")
    if process.returncode != 0:
        raise RuntimeError(
            f"command failed ({process.returncode}): {' '.join(cmd)}\n"
            f"stdout={stdout.decode(errors='replace')}\n"
            f"stderr={stderr.decode(errors='replace')}"
        )
    return stdout.decode("utf-8", errors="replace")


def compare_case(events: list[dict[str, Any]], slack_dump: dict[str, Any] | None) -> dict[str, Any]:
    db = analyze_db_events(events)
    slack = analyze_slack_dump(slack_dump) if slack_dump else None
    issues: list[str] = []

    if not db["terminal_output_count"]:
        issues.append("db_no_terminal_output")
    if db["execution_failed_count"]:
        issues.append("db_execution_failed")
    if db["execution_completed_before_terminal"]:
        issues.append("db_execution_completed_before_terminal_output")
    if db["nonzero_command_count"] and db["nonzero_command_count"] == db["commands_rendered_complete_count"]:
        issues.append("db_nonzero_commands_map_to_complete_tasks")

    if slack:
        if slack["raw_interactive_elements_count"]:
            issues.append("slack_raw_interactive_elements_fallback_text")
        if slack["empty_bot_message_count"]:
            issues.append("slack_empty_bot_message")
        if slack["visible_thinking_count"]:
            issues.append("slack_visible_thinking_text")
        if slack["tiny_fragment_count"]:
            issues.append("slack_tiny_fragment_message")
        if db["answer_text"] and not slack["final_response_present"]:
            issues.append("slack_missing_final_response_with_db_answer")
        if slack["plan_only_message_count"] and not slack["final_response_present"]:
            issues.append("slack_plan_only_thread_without_final")
        if slack["task_error_count"]:
            issues.append("slack_plan_task_error_status")
        if slack["task_output_error_count"] and not slack["final_response_present"]:
            issues.append("slack_error_output_without_final")

    return {
        "issues": sorted(set(issues)),
        "db": db,
        "slack": slack,
    }


def apply_case_expectations(summary: dict[str, Any], case: FuzzCase) -> None:
    issues = set(summary.get("issues") or [])
    final_text = ""
    slack = summary.get("slack")
    if isinstance(slack, dict):
        candidates = slack.get("final_text_candidates")
        if isinstance(candidates, list) and candidates:
            final_text = "\n".join(str(candidate) for candidate in candidates)
    if not final_text:
        db = summary.get("db")
        if isinstance(db, dict):
            final_text = str(db.get("answer_text") or "")

    for expected in case.expected_final_contains:
        if expected not in final_text:
            issues.add("slack_steering_followup_ignored" if case.followups else "expected_final_missing")
    for forbidden in case.forbidden_final_contains:
        if forbidden in final_text:
            issues.add("forbidden_final_present")
    if case.expect_max_duration_enforced:
        db = summary.get("db")
        if isinstance(db, dict) and db.get("terminal_output_count"):
            issues.add("api_max_duration_ignored")
    summary["issues"] = sorted(issues)


def analyze_db_events(events: list[dict[str, Any]]) -> dict[str, Any]:
    parsed_output: list[dict[str, Any]] = []
    terminal_ids: list[int] = []
    execution_completed_ids: list[int] = []
    execution_failed_ids: list[int] = []
    phase_by_item_id: dict[str, str] = {}
    current_phase: str | None = None
    answer_parts: list[str] = []
    nonzero_commands = 0
    commands_rendered_complete = 0

    for event in events:
        event_id = int(event["event_id"])
        event_type = str(event["event_type"])
        if event_type == "session.execution_completed":
            execution_completed_ids.append(event_id)
        if event_type == "session.execution_failed":
            execution_failed_ids.append(event_id)
        if event_type != "session.output.line":
            continue
        payload = event.get("payload")
        notification = parse_output_payload(payload)
        parsed_output.append({"event_id": event_id, "notification": notification})
        if not isinstance(notification, dict):
            terminal_ids.append(event_id)
            continue

        item = notification.get("item")
        item_id = str(notification.get("itemId") or notification.get("item_id") or (item or {}).get("id") or "")
        phase = str((item or {}).get("phase") or "").lower()
        if phase in ("commentary", "final_answer", "finalanswer"):
            normalized_phase = "final_answer" if phase == "finalanswer" else phase
            current_phase = normalized_phase
            if item_id:
                phase_by_item_id[item_id] = normalized_phase

        notification_type = str(notification.get("type") or "")
        if notification_type in ("turn.completed", "turn.failed", "turn.done", "result"):
            terminal_ids.append(event_id)
            terminal_text = terminal_result_text(notification)
            if terminal_text and not "".join(answer_parts).strip():
                answer_parts.append(terminal_text)

        if notification_type == "item.agentMessage.delta":
            delta = str(notification.get("delta") or "")
            phase_for_delta = phase_by_item_id.get(item_id) or current_phase
            if delta and phase_for_delta == "final_answer":
                answer_parts.append(delta)

        if notification_type == "item.completed" and isinstance(item, dict):
            item_type = str(item.get("type") or "")
            item_status = str(item.get("status") or "").lower()
            exit_code = item.get("exitCode", item.get("exit_code"))
            if item_type in ("commandExecution", "command_execution"):
                if isinstance(exit_code, int) and exit_code != 0:
                    nonzero_commands += 1
                    if item_status == "failed":
                        commands_rendered_complete += 1
                elif item_status == "failed":
                    nonzero_commands += 1
                    commands_rendered_complete += 1
            if item_type in ("agentMessage", "agent_message"):
                item_phase = phase_by_item_id.get(str(item.get("id") or "")) or current_phase
                text = str(item.get("text") or "")
                if text and item_phase == "final_answer" and not "".join(answer_parts).strip():
                    answer_parts.append(text)

    first_terminal = min(terminal_ids) if terminal_ids else None
    return {
        "event_count": len(events),
        "output_line_count": len(parsed_output),
        "terminal_output_count": len(terminal_ids),
        "first_terminal_event_id": first_terminal,
        "execution_completed_count": len(execution_completed_ids),
        "execution_failed_count": len(execution_failed_ids),
        "execution_completed_before_terminal": bool(
            first_terminal
            and execution_completed_ids
            and min(execution_completed_ids) < first_terminal
        ),
        "answer_text": "".join(answer_parts).strip(),
        "answer_length": len("".join(answer_parts).strip()),
        "nonzero_command_count": nonzero_commands,
        "commands_rendered_complete_count": commands_rendered_complete,
        "last_event_id": int(events[-1]["event_id"]) if events else None,
    }


def analyze_slack_dump(dump: dict[str, Any] | None) -> dict[str, Any]:
    if not dump:
        return {}
    messages = list(dump.get("messages") or [])
    replies = messages[1:] if messages else []
    auth = dump.get("auth") if isinstance(dump.get("auth"), dict) else {}
    requester_user_id = str(auth.get("user_id") or "")
    final_candidates: list[str] = []
    plan_only = 0
    task_errors = 0
    task_output_errors = 0
    plan_blocks = 0
    raw_interactive_elements = 0
    empty_bot_messages = 0
    visible_thinking = 0
    tiny_fragments = 0

    for message in replies:
        blocks = list(message.get("blocks") or [])
        plans = [block for block in blocks if block.get("type") == "plan"]
        is_assistant_reply = bool(plans) or (
            bool(message.get("bot_id")) and str(message.get("user") or "") != requester_user_id
        )
        message_text = str(message.get("text") or "")
        plan_blocks += len(plans)
        non_plan_text = "\n".join(
            text_from_block(block).strip() for block in blocks if block.get("type") != "plan"
        ).strip()
        if is_assistant_reply and message_text == "This message contains interactive elements.":
            raw_interactive_elements += 1
        if is_assistant_reply and not message_text.strip() and not blocks:
            empty_bot_messages += 1
        if is_assistant_reply and message_text.lstrip().startswith("*Thinking*"):
            visible_thinking += 1
        if is_assistant_reply and looks_like_tiny_fragment(message_text):
            tiny_fragments += 1
        if plans and not meaningful_final_text(non_plan_text):
            plan_only += 1
        if is_assistant_reply and meaningful_final_text(non_plan_text):
            final_candidates.append(non_plan_text)
        elif is_assistant_reply and meaningful_final_text(message_text):
            final_candidates.append(message_text)

        for plan in plans:
            for task in plan.get("tasks") or []:
                status = str(task.get("status") or "").lower()
                output_text = text_from_block(task.get("output") or {})
                detail_text = text_from_block(task.get("details") or {})
                if status == "error":
                    task_errors += 1
                if contains_errorish_text(output_text) or contains_errorish_text(detail_text):
                    task_output_errors += 1

    return {
        "message_count": len(messages),
        "reply_count": len(replies),
        "plan_block_count": plan_blocks,
        "plan_only_message_count": plan_only,
        "task_error_count": task_errors,
        "task_output_error_count": task_output_errors,
        "raw_interactive_elements_count": raw_interactive_elements,
        "empty_bot_message_count": empty_bot_messages,
        "visible_thinking_count": visible_thinking,
        "tiny_fragment_count": tiny_fragments,
        "final_response_present": bool(final_candidates),
        "final_text_candidates": final_candidates[-3:],
    }


def parse_output_payload(payload: Any) -> Any:
    if not isinstance(payload, str):
        return payload
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if isinstance(parsed, dict) and isinstance(parsed.get("method"), str):
        params = parsed.get("params") if isinstance(parsed.get("params"), dict) else {}
        return {**params, "type": str(parsed["method"]).replace("/", ".")}
    return parsed


def terminal_result_text(event: dict[str, Any]) -> str:
    for key in ("result", "result_text", "text", "final_text"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def meaningful_final_text(text: str) -> bool:
    normalized = text.strip()
    if not normalized:
        return False
    ignored = {
        "This message contains interactive elements.",
        "_base · codex_, with interactive elements",
        "base · codex",
    }
    if normalized in ignored:
        return False
    if normalized.endswith(", with interactive elements") and len(normalized) < 80:
        prefix = normalized.removesuffix(", with interactive elements").strip("_ ")
        return bool(prefix and prefix != "base · codex")
    return normalized.strip("_ ") != "base · codex"


def contains_errorish_text(text: str) -> bool:
    lower = text.lower()
    return any(token in lower for token in ("exit code", "error", "failed", "fatal:", "panic"))


def looks_like_tiny_fragment(text: str) -> bool:
    normalized = text.strip()
    if not normalized or len(normalized) > 12:
        return False
    if re.search(r"[A-Za-z0-9]{3,}", normalized):
        return False
    return bool(re.search(r"[?*_`\"'“”‘’]", normalized))


def text_from_block(block: Any) -> str:
    if isinstance(block, str):
        return block
    if isinstance(block, list):
        return "".join(text_from_block(item) for item in block)
    if not isinstance(block, dict):
        return ""
    parts: list[str] = []
    if isinstance(block.get("text"), str):
        parts.append(block["text"])
    for key in ("elements", "blocks", "fields"):
        value = block.get(key)
        if isinstance(value, list):
            parts.append("".join(text_from_block(item) for item in value))
    return "".join(parts)


def quote_path(value: str) -> str:
    return urllib.parse.quote(value, safe="")


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


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    sys.exit(main())
