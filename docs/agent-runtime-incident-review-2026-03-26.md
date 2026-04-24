# Agent Runtime Incident Review (2026-03-26)

## Scope

This review covers:

1. The specific Slack failure where a long-running task never returned a final response.
2. The concrete production root causes found via SSH/log/DB inspection.
3. Ranked bug/feature/workflow signals from recent data to drive redesign requirements.

Primary incident thread key: `C0A82R7S80N:1774483106.563569`

## Incident Timeline (UTC)

1. `2026-03-25T23:58:27.685Z`: `message_buffered` for thread.
2. `2026-03-25T23:58:27.721Z`: warm container claimed (`sandbox=b13a28be0967`).
3. `2026-03-25T23:58:27.760Z`: `/agent/execute` accepted.
4. `2026-03-25T23:58:27.765Z`: `turn_first_output` observed.
5. `2026-03-26T00:08:27.766Z`: `stream_eof` (first 600s disconnect).
6. `2026-03-26T00:08:29.774Z`: `/agent/reconnect` succeeds; output resumes.
7. Same pattern repeats at `00:18:29`, `00:28:33`, and `00:38:39`.
8. `2026-03-26T00:38:39.861Z` (SlackBot): `wire_reconnect_exhausted`.
9. `2026-03-26T00:38:39.978Z` (SlackBot): `slack_stream_fallback` with `message_not_in_streaming_state`.
10. No terminal assistant message was persisted for the thread (`system=1`, `user=1`, `assistant=0`).

## Production Evidence

1. Amp process was still alive in container while API stream repeatedly hit EOF every ~600s.
2. `/agent/reconnect` repeatedly succeeded but did not yield a durable terminal completion.
3. `chat_messages` for the incident thread contained no assistant rows.
4. `docker-socket-proxy` HAProxy defaults are `timeout client 10m` and `timeout server 10m`, matching observed periodic EOF cadence.
5. API reconciler emitted `reconcile_tick_error` every minute due schema mismatch while trying to set `state='suspended'`.

## Root Causes

1. **Stream path fragility under 10-minute idle attach timeout:** Docker attach wires were timing out while Amp continued running, forcing reconnect loops.
2. **Correctness coupled too tightly to live stream behavior:** Stream degradation/reconnect exhaustion could occur before durable terminal result/final Slack delivery.
3. **Reconciler poisoning from schema drift:** app code expected `suspended` state but DB constraint omitted it, causing repeated reconcile failures.

## Confirmed Schema Drift

1. `schema_migrations` in production contained only `001` through `004`.
2. `sandbox_sessions_state_check` allowed states only through `delivering` (no `suspended`).
3. Serving code attempted to write `suspended`, causing minute-by-minute reconcile failures.

## Repros You Can Share

Reference gist (CLI JSON input/output repros for Amp team):

1. https://gist.github.com/gakonst/e5223df32d11d2237e7f1d920f2c4171

### Repro A: Reconnect Loop Without Terminal Completion

1. Start an execution that can run silently for >10 minutes.
2. Observe API logs for periodic `stream_eof` approximately every 600 seconds.
3. Observe SlackBot logs for `wire_reconnecting`/`wire_reconnected` and eventual `wire_reconnect_exhausted`.
4. Verify no assistant row for thread in `chat_messages`.

### Repro B: Reconciler Poisoning On Missing State

1. Run code path that sets session state to `suspended`.
2. Use a DB constraint that does not include `suspended` in `sandbox_sessions_state_check`.
3. Observe `reconcile_tick_error` every reconcile interval and stalled reconcile progress for unrelated rows.

## Recent Bug Signals

Notes:

1. VictoriaLogs retains roughly 7 days, so warning/error event counts below are from retained log window.
2. `chat_messages` analysis covers full 14-day window.

### Top Runtime/Delivery Bugs (Ranked)

1. `reconcile_tick_error`: 8,166 warnings.
2. `tool_call_completed` failures: 842 warnings.
3. `stdin_broken_pipe`: 92 warnings.
4. SlackBot `set_title_failed`: 548 warnings (predominantly `no_permission`).
5. SlackBot `slack_stream_fallback`: 67 warnings.
6. SlackBot `wire_reconnect_exhausted`: 14 warnings.

### Missing Final Response Pattern

1. Threads with system prompt injected but no assistant output in last 14 days: `23 / 402` (`5.7%`).
2. Threads with `>=3` `stream_eof` events and no `turn_done` in retained logs: `10`.
3. Overall threads with user messages but no assistant messages in last 14 days: `60 / 551` (`10.9%`).

## Most Common Workflow Signals (14 Days)

From `chat_messages` user text and tool-call telemetry:

1. Document/attachment-heavy analysis (`pdf`, docs, sheets, decks): 197 user messages.
2. Research/analysis requests (`analyze`, `summary`, `overview`): 114 user messages.
3. Policy workflows (`whip count`, `senator`, `bill`, `congress`): 97 user messages.
4. Social/X workflows (`tweet`, `timeline`): 98 user messages.
5. Engineering/runtime tasks (`open pr`, `docker`, `runtime`, `api`): 163 user messages.

Top tool-call pairs:

1. `twitter.get_timeline`: 571 calls.
2. `private_db.db_query`: 402 calls.
3. `slack.search_messages`: 206 calls.
4. `private_db.bq_query`: 204 calls.
5. `websearch.search`: 187 calls.

## Most Common Feature Request Signals

Representative repeated asks in recent traffic:

1. Runtime reliability and baseline correctness (explicit asks to fix runtime stability and duplicate/missing outputs).
2. Better output shaping (clean table formatting, logos, richer memo/report formatting).
3. Capability expansion and automation (new skills, additional correlations/analytics, PR-oriented workflows).

## Redesign Requirements Added By This Incident

1. `/agent/execute` must always create final-delivery obligation row immediately (`awaiting_terminal`).
2. Watchdog deadlines (`silence_deadline`, `hard_deadline`) must force deterministic terminalization for stuck executions.
3. Final delivery must be independent from live stream lane.
4. Reconciler must be row-isolated and never fail globally from one bad row.
5. `readyz` must validate schema compatibility and fail closed on mismatch.

## Immediate Mitigations (Before Full Redesign)

1. Add schema-compatibility readiness checks and page on mismatch.
2. Isolate reconcile row failures to stop one poisoned row from breaking global reconcile.
3. Add stuck-execution watchdog with operator-visible terminal fallback.
4. Decouple Slack final response from stream liveness with durable outbox processing.
