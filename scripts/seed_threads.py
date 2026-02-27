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


def cid(prefix: str) -> str:
    """Generate a fake 64-char container ID."""
    return (prefix * 16)[:64]


# ---------------------------------------------------------------------------
# Mock sessions — diverse repos, harnesses, states
# ---------------------------------------------------------------------------
SESSIONS = [
    # 1. Reth performance investigation (amp, idle, 3h ago)
    {
        "slack_thread_key": "C07RETH001:1740100001.000001",
        "container_id": cid("a1b2c3"),
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-aaaa-7777-bbbb-111111111111",
        "state": "idle",
        "repo": "paradigmxyz/reth",
        "created_at": ts(10800),
        "last_activity": ts(9000),
    },
    # 2. Alloy bug fix (claude-code, working, 45min ago)
    {
        "slack_thread_key": "C07ALLOY01:1740200002.000002",
        "container_id": cid("b2c3d4"),
        "harness": "claude-code",
        "agent_thread_id": "session-cc-alloy-fix-22",
        "state": "working",
        "repo": "alloy-rs/alloy",
        "created_at": ts(2700),
        "last_activity": ts(30),
    },
    # 3. Foundry test suite (codex, idle, 1 day ago)
    {
        "slack_thread_key": "C07FOUND01:1740300003.000003",
        "container_id": cid("c3d4e5"),
        "harness": "codex",
        "agent_thread_id": "thread_codex_foundry_33",
        "state": "idle",
        "repo": "foundry-rs/foundry",
        "created_at": ts(86400),
        "last_activity": ts(82800),
    },
    # 4. AI v2 plugin development (amp, running, just spawned)
    {
        "slack_thread_key": "C07AIV201:1740400004.000004",
        "container_id": cid("d4e5f6"),
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-dddd-4444-eeee-444444444444",
        "state": "running",
        "repo": "paradigmxyz/ai_v2",
        "created_at": ts(180),
        "last_activity": ts(180),
    },
    # 5. Tempo node debugging (amp, idle, 6h ago)
    {
        "slack_thread_key": "C07TEMPO1:1740500005.000005",
        "container_id": cid("e5f6a1"),
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-5555-aaaa-cccc-555555555555",
        "state": "idle",
        "repo": "tempoxyz/tempo",
        "created_at": ts(21600),
        "last_activity": ts(18000),
    },
    # 6. Ithaca spec review (claude-code, idle, 2 days ago)
    {
        "slack_thread_key": "C07ITHAC1:1740600006.000006",
        "container_id": cid("f6a1b2"),
        "harness": "claude-code",
        "agent_thread_id": "session-cc-ithaca-review",
        "state": "idle",
        "repo": "ithacaxyz/odyssey",
        "created_at": ts(172800),
        "last_activity": ts(170000),
    },
    # 7. Multisig contract audit (amp, working, 20min ago)
    {
        "slack_thread_key": "C07MSIG01:1740700007.000007",
        "container_id": cid("a7b8c9"),
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-7777-bbbb-dddd-777777777777",
        "state": "working",
        "repo": "tempoxyz/multisig",
        "created_at": ts(3600),
        "last_activity": ts(1200),
    },
    # 8. Reth docs update (codex, stopped, 5 days ago)
    {
        "slack_thread_key": "C07RETH002:1740800008.000008",
        "container_id": cid("d8e9f0"),
        "harness": "codex",
        "agent_thread_id": "thread_codex_reth_docs",
        "state": "stopped",
        "repo": "paradigmxyz/reth",
        "created_at": ts(432000),
        "last_activity": ts(430000),
    },
    # 9. Commonware consensus (amp, idle, 12h ago)
    {
        "slack_thread_key": "C07CWARE1:1740900009.000009",
        "container_id": cid("a9b0c1"),
        "harness": "amp",
        "agent_thread_id": "T-019c8f00-9999-eeee-ffff-999999999999",
        "state": "idle",
        "repo": "commonwarexyz/monorepo",
        "created_at": ts(43200),
        "last_activity": ts(40000),
    },
    # 10. wevm/viem TypeScript migration (claude-code, working, 15min ago)
    {
        "slack_thread_key": "C07VIEM01:1741000010.000010",
        "container_id": cid("b0c1d2"),
        "harness": "claude-code",
        "agent_thread_id": "session-cc-viem-ts-10",
        "state": "working",
        "repo": "wevm/viem",
        "created_at": ts(1800),
        "last_activity": ts(900),
    },
]

# ---------------------------------------------------------------------------
# Mock turns — realistic multi-tool conversations
# ---------------------------------------------------------------------------
TURNS = [
    # =========================================================================
    # Session 1: Reth performance investigation (amp, 3 turns)
    # =========================================================================
    {
        "slack_thread_key": "C07RETH001:1740100001.000001",
        "turn_id": 1,
        "user_message": "Profile the block execution pipeline — where are we spending the most time?",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "T-019c8f00-aaaa-7777-bbbb-111111111111"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll profile the block execution pipeline. Let me start by finding the main execution entry point."},
                {"type": "tool_use", "name": "Grep", "id": "toolu_01", "input": {"pattern": "fn execute_block", "path": "crates/engine"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_01", "content": "crates/engine/src/executor.rs:142: pub fn execute_block(&mut self, block: &Block) -> Result<ExecutionResult> {\ncrates/engine/src/executor.rs:289: fn execute_block_with_state(&mut self, block: &Block, state: &mut State) -> Result<()> {"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Found the entry points. Let me read the main execution function and trace the hot path."},
                {"type": "tool_use", "name": "Read", "id": "toolu_02", "input": {"path": "crates/engine/src/executor.rs", "read_range": [142, 200]}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_02", "content": "pub fn execute_block(&mut self, block: &Block) -> Result<ExecutionResult> {\n    let state = self.state_provider.latest()?;\n    let mut cumulative_gas = 0u64;\n    \n    for tx in &block.body.transactions {\n        let result = self.execute_transaction(tx, &mut state)?;\n        cumulative_gas += result.gas_used;\n        self.receipts.push(result.receipt);\n    }\n    \n    let state_root = state.compute_root()?;  // <-- expensive\n    Ok(ExecutionResult { state_root, receipts: self.receipts.clone(), gas_used: cumulative_gas })\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "The block execution pipeline has three main phases:\n\n1. **Transaction execution** — iterates over all txs, each calling `execute_transaction`\n2. **Receipt collection** — accumulates gas and builds receipts\n3. **State root computation** — `state.compute_root()` at the end\n\nBased on typical Ethereum profiling, `compute_root()` (the state trie hash) is almost always the bottleneck — it's O(n) in the number of state changes and involves hashing. The `execute_transaction` calls are individually fast but add up for blocks with 200+ txs.\n\nI'd recommend adding `tracing::instrument` spans around each phase to get concrete numbers."},
            ]}},
            {"type": "result", "result": "Block execution has 3 phases: tx execution, receipt collection, and state root computation. State root (`compute_root()`) is likely the bottleneck — O(n) hashing over state changes."},
        ],
        "result": "Block execution has 3 phases: tx execution, receipt collection, and state root computation. State root (`compute_root()`) is likely the bottleneck.",
        "started_at": ts(10700),
        "finished_at": ts(10600),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 18.5,
    },
    {
        "slack_thread_key": "C07RETH001:1740100001.000001",
        "turn_id": 2,
        "user_message": "Add tracing spans to measure each phase. Use the tracing crate.",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll add `#[tracing::instrument]` and manual spans to the block execution pipeline."},
                {"type": "tool_use", "name": "edit_file", "id": "toolu_03", "input": {"path": "crates/engine/src/executor.rs", "old_str": "pub fn execute_block", "new_str": "#[tracing::instrument(skip(self), fields(block_num = block.header.number))]\npub fn execute_block"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_03", "content": "Applied edit to crates/engine/src/executor.rs"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Added tracing spans for:\n- `execute_block` (top-level, includes block number)\n- `execute_transactions` (inner span for the tx loop)\n- `compute_state_root` (inner span for the root computation)\n\nRun with `RUST_LOG=reth_engine=trace` to see timing."},
            ]}},
            {"type": "result", "result": "Added tracing::instrument spans to execute_block, tx loop, and compute_state_root. Use RUST_LOG=reth_engine=trace to see timing."},
        ],
        "result": "Added tracing spans to the execution pipeline. Use RUST_LOG=reth_engine=trace to see timing breakdowns.",
        "started_at": ts(10200),
        "finished_at": ts(10100),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 14.2,
    },
    {
        "slack_thread_key": "C07RETH001:1740100001.000001",
        "turn_id": 3,
        "user_message": "Now run the benchmarks and show me the results",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Running the block execution benchmarks with tracing enabled."},
                {"type": "tool_use", "name": "Bash", "id": "toolu_04", "input": {"cmd": "cd /home/agent/github/paradigmxyz/reth && cargo bench --bench engine -- --output-format=bencher 2>&1 | tail -20"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_04", "content": "test execute_block_100tx  ... bench:   2,340,128 ns/iter (+/- 145,000)\ntest execute_block_500tx  ... bench:  12,890,445 ns/iter (+/- 890,000)\ntest compute_state_root   ... bench:   8,456,233 ns/iter (+/- 567,000)\ntest execute_transaction  ... bench:      18,234 ns/iter (+/- 1,200)"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Benchmark results confirm the hypothesis:\n\n| Phase | Time | % of 500tx block |\n|---|---|---|\n| Single tx execution | 18μs | — |\n| 500 tx execution | 9.1ms (500 × 18μs) | 70.6% |\n| State root computation | 8.5ms | 65.6% |\n| Full block (500tx) | 12.9ms | 100% |\n\n**State root computation takes 65% of total block time.** This is the clear optimization target. Approaches:\n1. Parallel state root hashing (split trie into subtrees)\n2. Incremental root updates (avoid full recomputation)\n3. Lazy hashing (defer until needed for consensus)"},
            ]}},
            {"type": "result", "result": "Benchmarks show state root computation is 65% of block time. Main optimization targets: parallel hashing, incremental updates, or lazy computation."},
        ],
        "result": "State root computation is 65% of block execution time. Top optimization: parallel trie hashing.",
        "started_at": ts(9500),
        "finished_at": ts(9200),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 45.3,
    },

    # =========================================================================
    # Session 2: Alloy bug fix (claude-code, 2 turns)
    # =========================================================================
    {
        "slack_thread_key": "C07ALLOY01:1740200002.000002",
        "turn_id": 1,
        "user_message": "There's a panic in alloy-sol-types when parsing nested tuples. Can you find and fix it?",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "session-cc-alloy-fix-22"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll track down the panic in nested tuple parsing. Let me search for the relevant code."},
                {"type": "tool_use", "name": "Grep", "id": "toolu_05", "input": {"pattern": "fn parse_tuple|parse_params", "path": "crates/sol-types/src"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_05", "content": "crates/sol-types/src/abi/parser.rs:234: fn parse_tuple(&mut self, tokens: &[Token]) -> Result<DynSolType> {\ncrates/sol-types/src/abi/parser.rs:267: fn parse_params(&mut self, sig: &str) -> Result<Vec<DynSolType>> {"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Read", "id": "toolu_06", "input": {"path": "crates/sol-types/src/abi/parser.rs", "read_range": [234, 280]}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_06", "content": "fn parse_tuple(&mut self, tokens: &[Token]) -> Result<DynSolType> {\n    let mut types = Vec::new();\n    let mut depth = 0;\n    for token in tokens {\n        match token {\n            Token::OpenParen => depth += 1,\n            Token::CloseParen => {\n                depth -= 1;  // <-- can underflow!\n                if depth == 0 { break; }\n            }\n            Token::Type(t) => types.push(self.resolve_type(t)?),\n            _ => {}\n        }\n    }\n    Ok(DynSolType::Tuple(types))\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Found it! On line 241, `depth -= 1` can underflow if there's a `)` without a matching `(`. With `usize`, this causes a panic in debug builds and wraps in release. The fix is to check before decrementing."},
                {"type": "tool_use", "name": "edit_file", "id": "toolu_07", "input": {"path": "crates/sol-types/src/abi/parser.rs", "old_str": "depth -= 1;  // <-- can underflow!", "new_str": "depth = depth.checked_sub(1).ok_or_else(|| Error::msg(\"unmatched closing paren\"))?;"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_07", "content": "Applied edit to crates/sol-types/src/abi/parser.rs"},
            ]},
            {"type": "result", "result": "Fixed panic: `depth -= 1` underflows on unmatched `)`. Replaced with `checked_sub` that returns a descriptive error."},
        ],
        "result": "Fixed panic: depth underflow on unmatched closing paren in parse_tuple. Used checked_sub with descriptive error.",
        "started_at": ts(2600),
        "finished_at": ts(2400),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 31.0,
    },
    {
        "slack_thread_key": "C07ALLOY01:1740200002.000002",
        "turn_id": 2,
        "user_message": "Add a regression test for the nested tuple case",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll add a test that reproduces the original panic and verifies the fix."},
                {"type": "tool_use", "name": "create_file", "id": "toolu_08", "input": {"path": "crates/sol-types/src/abi/parser_test.rs", "content": "#[test]\nfn parse_nested_tuple_no_panic() {\n    // Previously panicked with depth underflow\n    let input = \"(uint256,(address,bool))\";\n    let result = DynSolType::parse(input);\n    assert!(result.is_ok());\n    \n    let ty = result.unwrap();\n    assert!(matches!(ty, DynSolType::Tuple(_)));\n}\n\n#[test]\nfn parse_unmatched_paren_returns_error() {\n    let input = \"uint256)\";\n    let result = DynSolType::parse(input);\n    assert!(result.is_err());\n    assert!(result.unwrap_err().to_string().contains(\"unmatched\"));\n}\n"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_08", "content": "Created crates/sol-types/src/abi/parser_test.rs"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Added two regression tests:\n1. `parse_nested_tuple_no_panic` — verifies `(uint256,(address,bool))` parses successfully\n2. `parse_unmatched_paren_returns_error` — verifies `uint256)` returns an error instead of panicking"},
            ]}},
            {"type": "result", "result": "Added 2 regression tests: nested tuple parsing succeeds, unmatched paren returns error instead of panic."},
        ],
        "result": "Added 2 regression tests for the nested tuple fix.",
        "started_at": ts(2200),
        "finished_at": ts(2100),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 12.8,
    },

    # =========================================================================
    # Session 3: Foundry test suite (codex, 2 turns)
    # =========================================================================
    {
        "slack_thread_key": "C07FOUND01:1740300003.000003",
        "turn_id": 1,
        "user_message": "Add fuzz tests for the Solidity compiler output parser in forge",
        "events": [
            {"type": "thread.started", "thread_id": "thread_codex_foundry_33"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "I'll create property-based fuzz tests for the Solidity compiler output parser. The parser needs to handle:\n\n1. Valid JSON compiler output (standard JSON input/output)\n2. Malformed JSON (shouldn't panic)\n3. Missing fields (graceful degradation)\n4. Extremely large outputs (memory safety)\n\nCreated `crates/forge/tests/fuzz_compiler_output.rs` with:\n- `fuzz_parse_valid_output` — generates random valid compiler JSON and verifies parsing\n- `fuzz_parse_garbage` — feeds arbitrary bytes and ensures no panics\n- `fuzz_parse_partial` — truncates valid output at random positions\n\nAll tests use `proptest` with 1000 cases. Run with `cargo test --test fuzz_compiler_output`."}},
            {"type": "turn.completed", "items": [
                {"type": "agent_message", "text": "Created fuzz tests for the Solidity compiler output parser."},
            ]},
        ],
        "result": "Created 3 proptest-based fuzz tests for the Solidity compiler output parser: valid output, garbage input, and truncated input.",
        "started_at": ts(85000),
        "finished_at": ts(84800),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 38.9,
    },
    {
        "slack_thread_key": "C07FOUND01:1740300003.000003",
        "turn_id": 2,
        "user_message": "Run the fuzz tests and tell me if anything breaks",
        "events": [
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Running the fuzz tests with 10,000 iterations each...\n\n```\nrunning 3 tests\ntest fuzz_parse_valid_output   ... ok (8.2s, 10000 cases)\ntest fuzz_parse_garbage        ... ok (3.1s, 10000 cases)\ntest fuzz_parse_partial        ... FAILED (case #4217)\n```\n\n**Found a bug!** `fuzz_parse_partial` found that truncating the compiler output right after the `\"abi\"` key but before its value causes an `unwrap()` panic in `parse_abi_array()` at `compiler/src/output.rs:89`.\n\nThe fix is to replace `json[\"abi\"].as_array().unwrap()` with a pattern match that returns an empty ABI on missing/malformed data. I've applied the fix and the tests now pass."}},
            {"type": "turn.completed", "items": [
                {"type": "agent_message", "text": "Fuzz tests found and fixed a panic in parse_abi_array."},
            ]},
        ],
        "result": "Fuzz tests found a panic when ABI JSON is truncated. Fixed unwrap() in parse_abi_array to handle malformed input gracefully.",
        "started_at": ts(84500),
        "finished_at": ts(84200),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 52.7,
    },

    # =========================================================================
    # Session 4: AI v2 plugin development (amp, 1 turn so far — just started)
    # =========================================================================
    {
        "slack_thread_key": "C07AIV201:1740400004.000004",
        "turn_id": 1,
        "user_message": "Create a new plugin for querying Dune Analytics dashboards",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "T-019c8f00-dddd-4444-eeee-444444444444"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll create a Dune Analytics plugin following the ai_v2 plugin conventions. Let me check an existing plugin for the pattern."},
                {"type": "tool_use", "name": "Read", "id": "toolu_09", "input": {"path": "plugins/defillama/client.py"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_09", "content": "class DefiLlamaClient:\n    \"\"\"Query DeFi TVL, yields, and protocol data from DefiLlama.\"\"\"\n    ..."},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Created `plugins/dune/` with:\n\n- **`client.py`** — `DuneClient` with methods:\n  - `execute_query(query_id, parameters)` — execute a Dune query by ID\n  - `get_results(execution_id)` — poll for results\n  - `search_dashboards(query, limit)` — search public dashboards\n  - `get_latest(query_id)` — get latest cached results\n- **`cli.py`** — Typer CLI wrapper\n- **`pyproject.toml`** — plugin metadata, depends on `httpx`\n- **`.env.example`** — documents `DUNE_API_KEY`"},
                {"type": "tool_use", "name": "create_file", "id": "toolu_10", "input": {"path": "plugins/dune/client.py", "content": "..."}},
            ]}},
            {"type": "result", "result": "Created plugins/dune/ with client.py (4 tools: execute_query, get_results, search_dashboards, get_latest), cli.py, pyproject.toml."},
        ],
        "result": "Created the Dune Analytics plugin with 4 tools: execute_query, get_results, search_dashboards, get_latest.",
        "started_at": ts(170),
        "finished_at": ts(90),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 28.4,
    },

    # =========================================================================
    # Session 5: Tempo node debugging (amp, 4 turns — long investigation)
    # =========================================================================
    {
        "slack_thread_key": "C07TEMPO1:1740500005.000005",
        "turn_id": 1,
        "user_message": "The tempo node is stuck at block 19,432,100 and not syncing further. Check the logs and find what's wrong.",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "T-019c8f00-5555-aaaa-cccc-555555555555"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Let me check the node logs for errors around that block height."},
                {"type": "tool_use", "name": "Bash", "id": "toolu_11", "input": {"cmd": "ssh ubuntu@dev-georgios 'journalctl -u tempo-node --since \"1 hour ago\" | grep -i \"error\\|panic\\|stuck\" | tail -20'"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_11", "content": "Feb 26 14:23:01 dev-georgios tempo[1234]: ERROR engine: Failed to validate block 19432101: missing parent state root\nFeb 26 14:23:01 dev-georgios tempo[1234]: ERROR engine: state_root=0xabc123...def456 expected=0x789abc...123def\nFeb 26 14:23:02 dev-georgios tempo[1234]: WARN  sync: Peer disconnected during state download: peer_id=16Uiu2...\nFeb 26 14:23:15 dev-georgios tempo[1234]: ERROR engine: Retrying block 19432101... attempt 47/50"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "The node is stuck because it can't validate block 19,432,101 — the parent state root doesn't match. This usually means the state trie is corrupted or a peer sent bad state data. The node is on attempt 47/50 of retrying.\n\nLet me check if this is a known issue with that specific block."},
            ]}},
            {"type": "result", "result": "Node stuck due to parent state root mismatch at block 19,432,101. State trie may be corrupted. Retry attempts nearly exhausted (47/50)."},
        ],
        "result": "State root mismatch at block 19,432,101. Likely corrupted state trie from bad peer data.",
        "started_at": ts(21500),
        "finished_at": ts(21300),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 22.0,
    },
    {
        "slack_thread_key": "C07TEMPO1:1740500005.000005",
        "turn_id": 2,
        "user_message": "Can we repair the state without a full resync?",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Yes, we can try a partial state repair. Let me check the healing mechanism."},
                {"type": "tool_use", "name": "Grep", "id": "toolu_12", "input": {"pattern": "fn heal_state|state_healing|repair_trie", "path": "crates/sync/src"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_12", "content": "crates/sync/src/healing.rs:45: pub async fn heal_state(&self, block_num: u64) -> Result<HealingReport> {"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Good news — there's a `heal_state` function in the sync module. You can trigger it via RPC:\n\n```bash\ncurl -X POST http://localhost:8545 \\\n  -H 'Content-Type: application/json' \\\n  -d '{\"jsonrpc\":\"2.0\",\"method\":\"debug_healState\",\"params\":[19432100],\"id\":1}'\n```\n\nThis will:\n1. Download the correct state trie nodes for block 19,432,100 from healthy peers\n2. Replace the corrupted nodes in the local DB\n3. Re-validate the state root\n4. Resume syncing from 19,432,101\n\nIt should take 5-10 minutes instead of hours for a full resync."},
            ]}},
            {"type": "result", "result": "Yes — use debug_healState RPC method to repair the trie for block 19,432,100. Takes 5-10 minutes vs full resync."},
        ],
        "result": "State can be repaired via debug_healState RPC. Downloads correct trie nodes from peers, ~5-10 min.",
        "started_at": ts(20800),
        "finished_at": ts(20600),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 16.5,
    },
    {
        "slack_thread_key": "C07TEMPO1:1740500005.000005",
        "turn_id": 3,
        "user_message": "Run the heal and monitor progress",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Running state healing for block 19,432,100..."},
                {"type": "tool_use", "name": "Bash", "id": "toolu_13", "input": {"cmd": "curl -s -X POST http://dev-georgios:8545 -H 'Content-Type: application/json' -d '{\"jsonrpc\":\"2.0\",\"method\":\"debug_healState\",\"params\":[19432100],\"id\":1}' | jq ."}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_13", "content": "{\n  \"jsonrpc\": \"2.0\",\n  \"id\": 1,\n  \"result\": {\n    \"status\": \"healing\",\n    \"block\": 19432100,\n    \"nodes_downloaded\": 0,\n    \"nodes_total\": 12847,\n    \"eta_seconds\": 420\n  }\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Healing started. 12,847 trie nodes to download, ETA ~7 minutes. Let me check progress after a bit..."},
                {"type": "tool_use", "name": "Bash", "id": "toolu_14", "input": {"cmd": "sleep 30 && curl -s http://dev-georgios:8545 -X POST -H 'Content-Type: application/json' -d '{\"jsonrpc\":\"2.0\",\"method\":\"debug_healState\",\"params\":[19432100],\"id\":1}' | jq .result"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_14", "content": "{\n  \"status\": \"healing\",\n  \"block\": 19432100,\n  \"nodes_downloaded\": 4219,\n  \"nodes_total\": 12847,\n  \"eta_seconds\": 280\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Progress: 4,219/12,847 nodes downloaded (32.8%), ETA ~4.5 minutes. The healing is working — once it completes, the node should automatically resume syncing."},
            ]}},
            {"type": "result", "result": "State healing in progress: 4,219/12,847 nodes (32.8%). ETA ~4.5 min. Node will auto-resume sync after completion."},
        ],
        "result": "State healing running: 32.8% done, ETA 4.5 min. Node will auto-resume sync.",
        "started_at": ts(20000),
        "finished_at": ts(19800),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 65.0,
    },
    {
        "slack_thread_key": "C07TEMPO1:1740500005.000005",
        "turn_id": 4,
        "user_message": "Is it syncing now?",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "tool_use", "name": "Bash", "id": "toolu_15", "input": {"cmd": "curl -s http://dev-georgios:8545 -X POST -H 'Content-Type: application/json' -d '{\"jsonrpc\":\"2.0\",\"method\":\"eth_syncing\",\"id\":1}' | jq .result"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_15", "content": "{\n  \"startingBlock\": \"0x128a464\",\n  \"currentBlock\": \"0x128a48f\",\n  \"highestBlock\": \"0x128b200\"\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Yes! The node is syncing again. Current block: 19,432,591 (it's past the stuck point at 19,432,101). Highest known block: 19,435,008. It's catching up at a healthy rate — should be fully synced within ~30 minutes."},
            ]}},
            {"type": "result", "result": "Node is syncing again. Current block 19,432,591 (past the stuck point). Fully synced ETA ~30 min."},
        ],
        "result": "Node is syncing! Current block 19,432,591, catching up to 19,435,008.",
        "started_at": ts(18500),
        "finished_at": ts(18400),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 8.2,
    },

    # =========================================================================
    # Session 6: Ithaca spec review (claude-code, 1 turn)
    # =========================================================================
    {
        "slack_thread_key": "C07ITHAC1:1740600006.000006",
        "turn_id": 1,
        "user_message": "Review the EIP-7702 implementation in odyssey and check for spec compliance",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "session-cc-ithaca-review"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll review the EIP-7702 (Set EOA Account Code) implementation against the spec."},
                {"type": "tool_use", "name": "finder", "id": "toolu_16", "input": {"query": "Find the EIP-7702 implementation: authorization list processing, set code transaction handling, and delegation logic"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_16", "content": "Found in:\n- crates/evm/src/eip7702.rs (main implementation)\n- crates/primitives/src/transaction/eip7702.rs (tx type)\n- crates/consensus/src/validation.rs (consensus checks)"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Reviewed 3 files against the EIP-7702 spec. Findings:\n\n✅ **Compliant:**\n- Authorization list processing correctly validates chain_id, nonce, and signature\n- Delegation designator prefix (0xef0100) correctly applied\n- Gas costs match spec (PER_AUTH_BASE_COST = 12500)\n- Empty code accounts correctly handled\n\n⚠️ **Issues found:**\n1. **Missing nonce increment** — spec requires the authority's nonce to be incremented after setting code, but `apply_authorization` doesn't do this (line 89)\n2. **Chain ID 0 handling** — spec says chain_id=0 means \"valid on any chain\", but the code rejects it with `InvalidChainId` (line 67)\n3. **Self-sponsoring edge case** — when the tx sender is also an authority in the list, the impl processes it twice (once as sender, once as authority)\n\nRecommend fixing issues 1 and 2 as they're spec violations. Issue 3 is an optimization."},
            ]}},
            {"type": "result", "result": "EIP-7702 review: mostly compliant. Found 2 spec violations (missing nonce increment, wrong chain_id=0 handling) and 1 optimization opportunity."},
        ],
        "result": "2 spec violations found: missing nonce increment after auth, incorrect chain_id=0 rejection.",
        "started_at": ts(171000),
        "finished_at": ts(170500),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 67.3,
    },

    # =========================================================================
    # Session 7: Multisig contract audit (amp, 2 turns)
    # =========================================================================
    {
        "slack_thread_key": "C07MSIG01:1740700007.000007",
        "turn_id": 1,
        "user_message": "Audit the MiniMultisig contract for security issues. Focus on the confirmation and execution flow.",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "T-019c8f00-7777-bbbb-dddd-777777777777"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll perform a security audit of the MiniMultisig contract, focusing on the confirmation and execution flow."},
                {"type": "tool_use", "name": "Read", "id": "toolu_17", "input": {"path": "contracts/src/MiniMultisig.sol"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_17", "content": "// SPDX-License-Identifier: MIT\npragma solidity ^0.8.24;\n\ncontract MiniMultisig {\n    mapping(bytes32 => uint256) public confirmations;\n    mapping(bytes32 => bool) public executed;\n    address[] public owners;\n    uint256 public threshold;\n    ...\n    function confirm(bytes32 txHash) external onlyOwner {\n        confirmations[txHash]++;\n    }\n    \n    function execute(bytes32 txHash, address to, uint256 value, bytes calldata data) external onlyOwner {\n        require(confirmations[txHash] >= threshold, \"not enough confirmations\");\n        require(!executed[txHash], \"already executed\");\n        executed[txHash] = true;\n        (bool success,) = to.call{value: value}(data);\n        require(success, \"execution failed\");\n    }\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Security audit findings for MiniMultisig:\n\n🔴 **Critical:**\n1. **Double-confirmation** — `confirm()` doesn't track which owners have confirmed. An owner can call `confirm()` multiple times to single-handedly reach threshold.\n   - Fix: Use `mapping(bytes32 => mapping(address => bool))` to track per-owner confirmations.\n\n2. **Frontrunning execution** — `execute()` takes the call parameters as arguments, but `confirm()` only confirms a hash. Nothing prevents an attacker from calling `execute()` with different parameters that produce the same hash (if the hash doesn't cover all params).\n   - Fix: Ensure `txHash = keccak256(abi.encode(to, value, data, nonce))`.\n\n🟡 **Medium:**\n3. **No nonce** — Without a nonce in the tx hash, the same transaction can be replayed after `executed` is reset (e.g., via an upgrade).\n4. **Reentrancy** — `executed[txHash] = true` is set before `to.call()`, which is correct (CEI pattern). ✅\n\n🟢 **Low:**\n5. **No event emission** — no events for `confirm` or `execute`, making off-chain tracking harder.\n6. **No confirmation revocation** — owners can't un-confirm."},
            ]}},
            {"type": "result", "result": "2 critical issues: double-confirmation (owner can confirm multiple times) and potential tx hash collision. 2 medium, 2 low."},
        ],
        "result": "Audit found 2 critical issues: double-confirmation vulnerability and hash collision risk. Plus 2 medium, 2 low.",
        "started_at": ts(3500),
        "finished_at": ts(3200),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 41.2,
    },
    {
        "slack_thread_key": "C07MSIG01:1740700007.000007",
        "turn_id": 2,
        "user_message": "Fix the double-confirmation bug",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Fixing the double-confirmation vulnerability by tracking per-owner confirmations."},
                {"type": "tool_use", "name": "edit_file", "id": "toolu_18", "input": {"path": "contracts/src/MiniMultisig.sol", "old_str": "mapping(bytes32 => uint256) public confirmations;", "new_str": "mapping(bytes32 => mapping(address => bool)) public hasConfirmed;\nmapping(bytes32 => uint256) public confirmationCount;"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_18", "content": "Applied edit to contracts/src/MiniMultisig.sol"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Updated the `confirm` function to check `hasConfirmed[txHash][msg.sender]` before incrementing, and the `execute` function to check `confirmationCount[txHash] >= threshold`. Each owner can now only confirm once per transaction."},
            ]}},
            {"type": "result", "result": "Fixed: added per-owner confirmation tracking with hasConfirmed mapping. Each owner can only confirm once."},
        ],
        "result": "Fixed double-confirmation: per-owner tracking via hasConfirmed mapping.",
        "started_at": ts(2000),
        "finished_at": ts(1800),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 19.7,
    },

    # =========================================================================
    # Session 8: Reth docs update (codex, 1 turn, stopped)
    # =========================================================================
    {
        "slack_thread_key": "C07RETH002:1740800008.000008",
        "turn_id": 1,
        "user_message": "Update the reth book to document the new --datadir flag and its interaction with --chain",
        "events": [
            {"type": "thread.started", "thread_id": "thread_codex_reth_docs"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "Updated the reth book documentation:\n\n1. **book/cli/node.md** — Added `--datadir` section explaining the flag, default values per OS, and interaction with `--chain` (data is stored in `{datadir}/{chain}/`).\n\n2. **book/installation/config.md** — Added a table showing default data directories:\n   - Linux: `~/.local/share/reth/{chain}`\n   - macOS: `~/Library/Application Support/reth/{chain}`\n   - Windows: `%APPDATA%\\reth\\{chain}`\n\n3. **book/run/mainnet.md** — Updated example commands to show `--datadir` usage with `--chain holesky` and `--chain mainnet`."}},
            {"type": "turn.completed", "items": [
                {"type": "agent_message", "text": "Updated 3 book files with --datadir documentation."},
            ]},
        ],
        "result": "Updated 3 reth book files: CLI reference, config docs, and mainnet guide with --datadir documentation.",
        "started_at": ts(431000),
        "finished_at": ts(430500),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 35.6,
    },

    # =========================================================================
    # Session 9: Commonware consensus (amp, 2 turns)
    # =========================================================================
    {
        "slack_thread_key": "C07CWARE1:1740900009.000009",
        "turn_id": 1,
        "user_message": "Explain how the simplex consensus module works in commonware. Focus on the voting rounds.",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "T-019c8f00-9999-eeee-ffff-999999999999"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Let me trace through the simplex consensus voting rounds."},
                {"type": "tool_use", "name": "finder", "id": "toolu_19", "input": {"query": "Find the simplex consensus implementation: proposal, prevote, precommit, and commit phases"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_19", "content": "Found in:\n- consensus/src/simplex/mod.rs (state machine)\n- consensus/src/simplex/voter.rs (vote handling)\n- consensus/src/simplex/types.rs (message types)"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "The simplex consensus works in 3 phases per round:\n\n**Phase 1: Propose** (leader only)\n- Round leader creates a `Proposal` containing the block and their view number\n- Broadcast to all validators\n- Timeout: 2s, after which validators move to prevote with `nil`\n\n**Phase 2: Prevote** (all validators)\n- On receiving a valid proposal: send `Prevote(block_hash)`\n- On timeout: send `Prevote(nil)`\n- Wait for 2f+1 prevotes for the same value (where f = max faults)\n\n**Phase 3: Precommit** (all validators)\n- If received 2f+1 prevotes for a block: send `Precommit(block_hash)`\n- If received 2f+1 prevotes for nil: send `Precommit(nil)`\n- On 2f+1 precommits for a block → **commit** and advance to next round\n- On 2f+1 precommits for nil → advance round without committing\n\nThe state machine in `mod.rs` tracks `(view, round, phase)` and transitions are driven by message arrival + timeouts. Lock and unlock rules prevent equivocation."},
            ]}},
            {"type": "result", "result": "Simplex consensus: 3 phases (propose → prevote → precommit) with 2f+1 quorum. Lock rules prevent equivocation."},
        ],
        "result": "Simplex consensus has 3 phases with 2f+1 quorum thresholds and lock-based equivocation prevention.",
        "started_at": ts(42000),
        "finished_at": ts(41500),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 34.8,
    },
    {
        "slack_thread_key": "C07CWARE1:1740900009.000009",
        "turn_id": 2,
        "user_message": "What happens when a leader is Byzantine? Walk through the recovery path.",
        "events": [
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Here's the Byzantine leader recovery flow:\n\n**Scenario:** Leader for round R is Byzantine — sends different proposals to different validators (equivocation) or sends nothing.\n\n**Case 1: Leader sends nothing**\n1. Propose timeout fires (2s)\n2. All honest validators prevote `nil`\n3. 2f+1 nil prevotes → all precommit `nil`\n4. 2f+1 nil precommits → round R ends without commit\n5. View change: increment round to R+1, new leader selected via `leader = validators[(view + round) % n]`\n\n**Case 2: Leader equivocates (sends different blocks)**\n1. Validators receive conflicting proposals\n2. Honest validators detect equivocation via `seen_proposals` set\n3. On detecting equivocation: prevote `nil` (even if first proposal was valid)\n4. Byzantine validators might prevote for their preferred block\n5. At most f Byzantine prevotes for block B — not enough for 2f+1 quorum\n6. Round times out → same recovery as Case 1\n\n**Key safety property:** No block can be committed without 2f+1 precommits. Since there are at most f Byzantine validators, they can't unilaterally commit anything. The lock mechanism ensures that if any honest validator locked on block B, they won't precommit for B' in later rounds until they see 2f+1 prevotes for B'."},
            ]}},
            {"type": "result", "result": "Byzantine leader recovery: timeout → nil votes → round skip → new leader. Equivocation detected via seen_proposals, triggers nil prevote. Safety guaranteed by 2f+1 quorum."},
        ],
        "result": "Byzantine leader triggers timeout or equivocation detection, both lead to nil votes and round advancement. Safety from 2f+1 quorum requirement.",
        "started_at": ts(41000),
        "finished_at": ts(40500),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 28.1,
    },

    # =========================================================================
    # Session 10: wevm/viem TypeScript migration (claude-code, 1 turn so far)
    # =========================================================================
    {
        "slack_thread_key": "C07VIEM01:1741000010.000010",
        "turn_id": 1,
        "user_message": "Migrate the legacy getBalance action to use the new TypeScript 5.5 satisfies operator and add stricter return types",
        "events": [
            {"type": "system", "subtype": "init", "session_id": "session-cc-viem-ts-10"},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "I'll migrate `getBalance` to use TS 5.5 patterns. Let me read the current implementation."},
                {"type": "tool_use", "name": "Read", "id": "toolu_20", "input": {"path": "src/actions/public/getBalance.ts"}},
            ]}},
            {"type": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_20", "content": "import type { Client, Transport, Chain, Account } from '../../types'\nimport { formatEther } from '../../utils'\n\nexport type GetBalanceParameters = {\n  address: `0x${string}`\n  blockTag?: 'latest' | 'pending' | 'earliest'\n  blockNumber?: bigint\n}\n\nexport type GetBalanceReturnType = bigint\n\nexport async function getBalance<TChain extends Chain | undefined>(\n  client: Client<Transport, TChain>,\n  { address, blockTag, blockNumber }: GetBalanceParameters\n): Promise<GetBalanceReturnType> {\n  const balance = await client.request({\n    method: 'eth_getBalance',\n    params: [address, blockNumber ? `0x${blockNumber.toString(16)}` : blockTag ?? 'latest'],\n  })\n  return BigInt(balance)\n}"},
            ]},
            {"type": "assistant", "message": {"role": "assistant", "content": [
                {"type": "text", "text": "Migrated `getBalance` with these changes:\n\n1. **`satisfies` for parameter validation** — `GetBalanceParameters` now uses `satisfies ActionParameters` to ensure it conforms to the base action interface at the type level\n2. **Stricter return type** — `GetBalanceReturnType` is now a branded type `bigint & { __brand: 'wei' }` to prevent mixing wei/gwei/ether values\n3. **Block parameter union** — replaced separate `blockTag`/`blockNumber` with a discriminated union `BlockParameter` to prevent passing both\n4. **Const type parameters** — added `as const satisfies` for the RPC method string literal"},
            ]}},
            {"type": "result", "result": "Migrated getBalance to TS 5.5: satisfies for param validation, branded bigint return type, discriminated union for block params."},
        ],
        "result": "Migrated getBalance to TypeScript 5.5 patterns: satisfies operator, branded return types, discriminated union params.",
        "started_at": ts(1700),
        "finished_at": ts(1400),
        "exit_code": 0,
        "timed_out": False,
        "duration_s": 42.5,
    },
]


def main():
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            # Clear existing mock data
            cur.execute("DELETE FROM agent_turns")
            cur.execute("DELETE FROM agent_sessions")
            print("Cleared existing data.\n")

            # Insert sessions
            for s in SESSIONS:
                cur.execute(
                    """
                    INSERT INTO agent_sessions
                        (slack_thread_key, container_id, harness, agent_thread_id,
                         state, repo, created_at, last_activity)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (slack_thread_key) DO UPDATE SET
                        container_id    = EXCLUDED.container_id,
                        harness         = EXCLUDED.harness,
                        agent_thread_id = EXCLUDED.agent_thread_id,
                        state           = EXCLUDED.state,
                        repo            = EXCLUDED.repo,
                        last_activity   = EXCLUDED.last_activity
                    """,
                    (
                        s["slack_thread_key"],
                        s["container_id"],
                        s["harness"],
                        s["agent_thread_id"],
                        s["state"],
                        s.get("repo"),
                        s["created_at"],
                        s["last_activity"],
                    ),
                )
                print(f"  ✓ session {s['slack_thread_key']} ({s['harness']}, {s['state']}, {s.get('repo', 'no repo')})")

            print()

            # Insert turns
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
                print(f"  ✓ turn {t['slack_thread_key']} #{t['turn_id']}: {t['user_message'][:60]}")

        conn.commit()
        print(f"\nSeeded {len(SESSIONS)} sessions and {len(TURNS)} turns.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
