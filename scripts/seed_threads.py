#!/usr/bin/env python3
"""Seed agent_sessions and agent_turns with realistic mock data.

Usage:
    uv run python scripts/seed_threads.py              # uses DATABASE_URL from .env
    DATABASE_URL=postgresql://... python scripts/seed_threads.py
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# Load .env from repo root
env_path = Path(__file__).resolve().parent.parent / ".env"
if env_path.exists():
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

import psycopg2
import psycopg2.extras

DATABASE_URL = os.environ.get("DATABASE_URL", "")
if not DATABASE_URL:
    print("ERROR: DATABASE_URL not set", file=sys.stderr)
    sys.exit(1)

now = time.time()


def ts(offset_seconds: float) -> datetime:
    return datetime.fromtimestamp(now - offset_seconds, tz=timezone.utc)


# ---------------------------------------------------------------------------
# Mock sessions
# ---------------------------------------------------------------------------
SESSIONS = [
    {
        "slack_thread_key": "C07DEMO001:1740000001.000001",
        "container_id": "a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6abcd",
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-aaaa-7777-bbbb-111111111111",
        "state": "idle",
        "created_at": ts(3600),      # 1 hour ago
        "last_activity": ts(600),    # 10 min ago
    },
    {
        "slack_thread_key": "C07DEMO002:1740000002.000002",
        "container_id": "b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6abcd",
        "harness": "claude-code",
        "agent_thread_id": "session-cc-22222222",
        "state": "working",
        "created_at": ts(7200),      # 2 hours ago
        "last_activity": ts(30),     # 30s ago
    },
    {
        "slack_thread_key": "C07DEMO003:1740000003.000003",
        "container_id": "c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6abcd",
        "harness": "codex",
        "agent_thread_id": "thread_codex_33333333",
        "state": "idle",
        "created_at": ts(86400),     # 1 day ago
        "last_activity": ts(1800),   # 30 min ago
    },
    {
        "slack_thread_key": "C07DEMO004:1740000004.000004",
        "container_id": "d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6abcd",
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-dddd-4444-eeee-444444444444",
        "state": "running",
        "created_at": ts(120),       # 2 min ago
        "last_activity": ts(120),    # 2 min ago (just spawned)
    },
]

# ---------------------------------------------------------------------------
# Mock turns with realistic events
# ---------------------------------------------------------------------------
TURNS = [
    # --- Session 1 (amp): two completed turns ---
    {
        "slack_thread_key": "C07DEMO001:1740000001.000001",
        "turn_id": 1,
        "user_message": "Read the README and summarize what this project does",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "T-019c8f00-aaaa-7777-bbbb-111111111111"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll read the README to understand the project."},
                {"type": "tool_use", "name": "Read", "id": "toolu_01A1B2C3D4", "input": {"path": "/home/agent/github/tempo/README.md"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01A1B2C3D4", "content": "# Tempo\\n\\nA high-performance Ethereum execution client..."},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "This is Tempo, a high-performance Ethereum execution client built in Rust. It focuses on speed and reliability for block execution and state management."},
            ]}},
            {"type": "result", "result": "Tempo is a high-performance Ethereum execution client built in Rust, focused on fast block execution and reliable state management."},
        ],
        "result": "Tempo is a high-performance Ethereum execution client built in Rust, focused on fast block execution and reliable state management.",
        "started_at": ts(3500),
        "finished_at": ts(3400),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 12.3,
    },
    {
        "slack_thread_key": "C07DEMO001:1740000001.000001",
        "turn_id": 2,
        "user_message": "Find all TODO comments in the codebase and list them",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll search for TODO comments across the codebase."},
                {"type": "tool_use", "name": "Grep", "id": "toolu_02E5F6G7H8", "input": {"pattern": "TODO", "path": "/home/agent/github/tempo/src"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_02E5F6G7H8", "content": "src/engine.rs:42: // TODO: optimize state root calculation\nsrc/p2p/sync.rs:189: // TODO: add retry logic for peer disconnects\nsrc/rpc/server.rs:77: // TODO: implement eth_getLogs pagination"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Found 3 TODO comments:\n\n1. **src/engine.rs:42** — optimize state root calculation\n2. **src/p2p/sync.rs:189** — add retry logic for peer disconnects\n3. **src/rpc/server.rs:77** — implement eth_getLogs pagination"},
            ]}},
            {"type": "result", "result": "Found 3 TODO comments in the codebase across engine, p2p sync, and RPC modules."},
        ],
        "result": "Found 3 TODO comments in the codebase across engine, p2p sync, and RPC modules.",
        "started_at": ts(3000),
        "finished_at": ts(2950),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 8.7,
    },
    # --- Session 2 (claude-code): one turn, currently working on second ---
    {
        "slack_thread_key": "C07DEMO002:1740000002.000002",
        "turn_id": 1,
        "user_message": "Set up a new Rust benchmark for the state trie",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "session-cc-22222222"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll create a benchmark for the state trie implementation."},
                {"type": "tool_use", "name": "Read", "id": "toolu_03I9J0K1L2", "input": {"path": "/home/agent/github/tempo/benches"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_03I9J0K1L2", "content": "criterion_main.rs\nengine_bench.rs"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": "toolu_04M3N4O5P6", "input": {"path": "/home/agent/github/tempo/benches/engine_bench.rs"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_04M3N4O5P6", "content": "use criterion::{criterion_group, criterion_main, Criterion};\n\nfn bench_execute_block(c: &mut Criterion) {\n    // ...\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I see the existing benchmark structure. Let me create a new file for the state trie benchmark."},
                {"type": "tool_use", "name": "create_file", "id": "toolu_05Q7R8S9T0", "input": {"path": "/home/agent/github/tempo/benches/trie_bench.rs", "content": "use criterion::{criterion_group, Criterion};\nuse tempo_trie::PatriciaTrie;\n\nfn bench_trie_insert(c: &mut Criterion) {\n    c.bench_function(\"trie_insert_1000\", |b| {\n        b.iter(|| {\n            let mut trie = PatriciaTrie::new();\n            for i in 0..1000 {\n                trie.insert(format!(\"key_{i}\").as_bytes(), b\"value\");\n            }\n        })\n    });\n}\n\ncriterion_group!(benches, bench_trie_insert);\n"}},
            ]}},
            {"type": "result", "result": "Created benches/trie_bench.rs with a Patricia trie insertion benchmark (1000 keys). You can run it with `cargo bench -- trie`."},
        ],
        "result": "Created benches/trie_bench.rs with a Patricia trie insertion benchmark (1000 keys). Run with `cargo bench -- trie`.",
        "started_at": ts(5000),
        "finished_at": ts(4900),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 22.1,
    },
    # --- Session 3 (codex): one completed turn ---
    {
        "slack_thread_key": "C07DEMO003:1740000003.000003",
        "turn_id": 1,
        "user_message": "Explain the gas metering implementation",
        "events": [
            {"type": "thread.started", "thread_id": "thread_codex_33333333"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "The gas metering in Tempo uses a table-driven approach where each EVM opcode has a pre-computed gas cost. The implementation is in `src/evm/gas.rs` and uses a const array indexed by opcode byte value.\n\nKey design decisions:\n- Static gas costs are looked up in O(1) via array indexing\n- Dynamic gas (SSTORE, memory expansion) is computed inline\n- Gas refunds are tracked separately and applied post-execution\n- The interpreter checks remaining gas before each instruction via an inline `charge_gas!` macro"}},
            {"type": "turn.completed", "items": [
                {"type": "agent_message", "text": "The gas metering system uses table-driven opcode costs with O(1) lookup."},
            ]},
        ],
        "result": "The gas metering in Tempo uses a table-driven approach with O(1) opcode cost lookups, inline dynamic gas computation, and post-execution refund application.",
        "started_at": ts(2000),
        "finished_at": ts(1900),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 15.4,
    },
]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            # Upsert sessions
            for s in SESSIONS:
                cur.execute(
                    """
                    INSERT INTO agent_sessions
                        (slack_thread_key, container_id, harness, agent_thread_id,
                         state, created_at, last_activity)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (slack_thread_key) DO UPDATE SET
                        container_id    = EXCLUDED.container_id,
                        harness         = EXCLUDED.harness,
                        agent_thread_id = EXCLUDED.agent_thread_id,
                        state           = EXCLUDED.state,
                        last_activity   = EXCLUDED.last_activity
                    """,
                    (
                        s["slack_thread_key"],
                        s["container_id"],
                        s["harness"],
                        s["agent_thread_id"],
                        s["state"],
                        s["created_at"],
                        s["last_activity"],
                    ),
                )
                print(f"  ✓ session {s['slack_thread_key']} ({s['harness']}, {s['state']})")

            # Upsert turns
            for t in TURNS:
                cur.execute(
                    """
                    INSERT INTO agent_turns
                        (slack_thread_key, turn_id, user_message, events, result,
                         started_at, finished_at, exit_code, timed_out, duration_s)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (slack_thread_key, turn_id) DO UPDATE SET
                        events      = EXCLUDED.events,
                        result      = EXCLUDED.result,
                        finished_at = EXCLUDED.finished_at,
                        exit_code   = EXCLUDED.exit_code,
                        timed_out   = EXCLUDED.timed_out,
                        duration_s  = EXCLUDED.duration_s
                    """,
                    (
                        t["slack_thread_key"],
                        t["turn_id"],
                        t["user_message"],
                        json.dumps(t["events"]),
                        t["result"],
                        t["started_at"],
                        t["finished_at"],
                        t["exit_code"],
                        t["timed_out"],
                        t["duration_s"],
                    ),
                )
                print(f"  ✓ turn {t['slack_thread_key']} #{t['turn_id']}")

        conn.commit()
        print(f"\nSeeded {len(SESSIONS)} sessions and {len(TURNS)} turns.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
