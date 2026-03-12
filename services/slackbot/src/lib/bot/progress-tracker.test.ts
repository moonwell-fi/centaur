/**
 * Tests for ProgressTracker using realistic Amp NDJSON event sequences.
 *
 * Run:  node --experimental-strip-types services/slackbot/src/lib/bot/progress-tracker.test.ts
 *
 * These are the CanonicalEvent shapes (post-normalization) that the tracker
 * consumes — NOT the raw SSE payloads.  Each test simulates a realistic Amp
 * turn by feeding events in the order Amp actually emits them.
 */

import assert from "node:assert/strict";

// ── Minimal type stubs so we don't need workspace/external deps ─────────────

type ContentBlock =
  | { type: "text"; text: string }
  | { type: "tool_use"; id: string; name: string; input: Record<string, unknown> }
  | { type: "tool_result"; tool_use_id: string; content: unknown; is_error: boolean };

type SubagentActivity = { description: string; toolName?: string };

type CanonicalEvent =
  | { type: "assistant"; message: { content: ContentBlock[] } }
  | { type: "tool"; content: Array<{ tool_use_id: string; content: unknown; is_error: boolean }> }
  | { type: "reasoning"; text: string }
  | { type: "subagent"; status: string; subagent_id: string; name?: string; summary?: string; error?: string; activity?: string; activities?: SubagentActivity[] }
  | { type: "result"; text: string }
  | { type: "error"; error: string }
  | { type: "system"; subtype: string; session_id?: string }
  | { type: "usage"; usage: Record<string, unknown>; model?: string; authoritative?: boolean };

type StreamChunk =
  | { type: "task_update"; id: string; title: string; status: string }
  | { type: "markdown_text"; text: string };

// ── Inline ProgressTracker (mirrors the real one but avoids import issues) ──
// We need to test the actual file.  To avoid path-alias issues with bare
// `node --experimental-strip-types`, we re-export a factory that constructs
// the class with the same logic.  If the implementation drifts, update this
// copy or switch to a bundler-based runner.

// Actually — let's just copy the class logic inline so the test is
// self-contained AND tests the exact algorithm.  The real file is the source
// of truth; this is a snapshot test of the algorithm.

// ---------- BEGIN: algorithm under test (keep in sync) ----------

type ActiveTool = { name: string; input: Record<string, unknown>; startedAt: number };

const MAX_VISIBLE_STEPS = 5;

type HistoryEntry = { toolId: string; title: string; status: string };

class ProgressTracker {
  lastAssistantText = "";
  resultText = "";
  private activeTools = new Map<string, ActiveTool>();
  private _pendingChunks: StreamChunk[] = [];
  private initCompleted = false;
  private stepHistory: HistoryEntry[] = [];

  private emitVisibleWindow(): void {
    const start = Math.max(0, this.stepHistory.length - MAX_VISIBLE_STEPS);
    for (let i = start; i < this.stepHistory.length; i++) {
      const entry = this.stepHistory[i];
      this._pendingChunks.push({ type: "task_update", id: `step-${i - start}`, title: entry.title, status: entry.status });
    }
  }

  private emitSlot(historyIndex: number): void {
    const windowStart = Math.max(0, this.stepHistory.length - MAX_VISIBLE_STEPS);
    const slotIndex = historyIndex - windowStart;
    if (slotIndex < 0 || slotIndex >= MAX_VISIBLE_STEPS) return;
    const entry = this.stepHistory[historyIndex];
    this._pendingChunks.push({ type: "task_update", id: `step-${slotIndex}`, title: entry.title, status: entry.status });
  }

  private addStep(toolId: string, title: string, status: string): void {
    this.stepHistory.push({ toolId, title, status });
    if (this.stepHistory.length > MAX_VISIBLE_STEPS) {
      this.emitVisibleWindow();
    } else {
      this.emitSlot(this.stepHistory.length - 1);
    }
  }

  private updateStep(toolId: string, title: string, status: string): void {
    const idx = this.stepHistory.findLastIndex((e) => e.toolId === toolId);
    if (idx === -1) return;
    this.stepHistory[idx].title = title;
    this.stepHistory[idx].status = status;
    this.emitSlot(idx);
  }

  update(event: CanonicalEvent): boolean {
    if (!this.initCompleted) {
      this.initCompleted = true;
      this._pendingChunks.push({ type: "task_update", id: "init", title: "Started", status: "complete" });
    }
    if (event.type === "assistant" && event.message?.content) {
      let changed = false;
      let textInThisEvent = "";
      for (const block of event.message.content) {
        if (block.type === "tool_use") {
          this.lastAssistantText = "";
          this.activeTools.set(block.id, { name: block.name, input: block.input, startedAt: Date.now() });
          changed = true;
          this.addStep(block.id, block.name, "in_progress");
        } else if (block.type === "text" && block.text) {
          textInThisEvent = block.text;
        }
      }
      if (textInThisEvent && this.activeTools.size === 0) {
        this.lastAssistantText = textInThisEvent;
      }
      return changed;
    }

    if (event.type === "tool" && event.content) {
      let changed = false;
      for (const block of event.content) {
        if (this.activeTools.has(block.tool_use_id)) {
          this.activeTools.delete(block.tool_use_id);
          changed = true;
          this.updateStep(block.tool_use_id, "done", block.is_error ? "error" : "complete");
        }
      }
      return changed;
    }

    if (event.type === "subagent") {
      if (event.status === "started") {
        this.addStep(event.subagent_id, event.name || "Subagent", "in_progress");
        return true;
      }
      if (event.status === "completed" || event.status === "failed") {
        this.updateStep(event.subagent_id, event.name || "Subagent", event.status === "completed" ? "complete" : "error");
        return true;
      }
      return false;
    }

    if (event.type === "result") {
      this.resultText = event.text;
      return true;
    }

    if (event.type === "error") {
      this._pendingChunks.push({ type: "markdown_text", text: `Error: ${event.error}` });
      return true;
    }

    return false;
  }

  addHandoff(goal: string, _newThreadKey: string): void {
    this.activeTools.clear();
    this.lastAssistantText = "";
    this.resultText = "";
    this.addStep(`handoff-${Date.now()}`, `Handed off → ${goal}`, "complete");
  }

  pendingChunks(): StreamChunk[] {
    const c = this._pendingChunks;
    this._pendingChunks = [];
    return c;
  }
}

// ---------- END: algorithm under test ----------

/** Helper: simulate what bot.ts does to get the final message */
function finalMessage(t: ProgressTracker): string {
  return (t.resultText || t.lastAssistantText).trim();
}

// ═══════════════════════════════════════════════════════════════════════════
// Test helpers
// ═══════════════════════════════════════════════════════════════════════════

let passed = 0;
let failed = 0;

function test(name: string, fn: () => void) {
  try {
    fn();
    passed++;
    console.log(`  ✓ ${name}`);
  } catch (e: any) {
    failed++;
    console.log(`  ✗ ${name}`);
    console.log(`    ${e.message}`);
  }
}

// ═══════════════════════════════════════════════════════════════════════════
// 1. Simple text-only response (no tools)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Simple text response ──");

test("text-only assistant message captured as final", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Here is your answer." }] } });
  assert.equal(t.lastAssistantText, "Here is your answer.");
  assert.equal(finalMessage(t), "Here is your answer.");
});

test("multiple text events — last one wins", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "First part." }] } });
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Second part." }] } });
  assert.equal(t.lastAssistantText, "Second part.");
});

// ═══════════════════════════════════════════════════════════════════════════
// 2. THE BUG: preamble text before a tool call (separate events)
//    Amp says "Let me look at the chat..." then calls read_thread
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Preamble before tool call (separate events — the bug) ──");

test("preamble text cleared when tool_use starts (separate events)", () => {
  const t = new ProgressTracker();
  // Amp emits text first
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me look at the chat history..." }] } });
  assert.equal(t.lastAssistantText, "Let me look at the chat history...");

  // Then emits tool_use in a separate event
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "read_thread", input: { threadID: "T-abc" } }] } });
  assert.equal(t.lastAssistantText, "", "tool_use should clear preamble text");

  // Stream dies here (EOF without tool result or turn.done)
  assert.equal(finalMessage(t), "", "should NOT post preamble as final message");
});

test("preamble text cleared when tool_use starts (same event)", () => {
  const t = new ProgressTracker();
  // Amp emits text + tool_use in one event (co-located)
  t.update({
    type: "assistant",
    message: {
      content: [
        { type: "text", text: "Let me search for that..." },
        { type: "tool_use", id: "t1", name: "finder", input: { query: "auth" } },
      ],
    },
  });
  assert.equal(t.lastAssistantText, "", "preamble in same event as tool_use should be discarded");
  assert.equal(finalMessage(t), "");
});

// ═══════════════════════════════════════════════════════════════════════════
// 3. Full tool-call cycle: preamble → tool → result → final text
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Full tool call cycle ──");

test("final text after tool completes is captured correctly", () => {
  const t = new ProgressTracker();

  // 1. Preamble
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me check..." }] } });

  // 2. Tool use
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/foo" } }] } });
  assert.equal(t.lastAssistantText, "", "cleared by tool_use");

  // 3. Tool result
  t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "file contents", is_error: false }] });

  // 4. Final answer
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "The file contains X." }] } });
  assert.equal(t.lastAssistantText, "The file contains X.");
  assert.equal(finalMessage(t), "The file contains X.");
});

test("multiple tool calls in sequence — final text after all tools", () => {
  const t = new ProgressTracker();

  // Tool 1
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "I'll read two files." }] } });
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/a" } }] } });
  assert.equal(t.lastAssistantText, "");
  t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "aaa", is_error: false }] });

  // Tool 2
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t2", name: "Read", input: { path: "/b" } }] } });
  t.update({ type: "tool", content: [{ tool_use_id: "t2", content: "bbb", is_error: false }] });

  // Final answer
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Both files look good." }] } });
  assert.equal(finalMessage(t), "Both files look good.");
});

// ═══════════════════════════════════════════════════════════════════════════
// 4. Parallel tool calls (Amp emits multiple tool_use in one event)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Parallel tool calls ──");

test("parallel tool calls — text blocked until all complete", () => {
  const t = new ProgressTracker();

  // Two tool_use in one event
  t.update({
    type: "assistant",
    message: {
      content: [
        { type: "tool_use", id: "t1", name: "Read", input: { path: "/a" } },
        { type: "tool_use", id: "t2", name: "Read", input: { path: "/b" } },
      ],
    },
  });

  // First tool completes — still one active
  t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "a", is_error: false }] });

  // Amp emits intermediate text while t2 is still running
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Got the first file, waiting..." }] } });
  assert.equal(t.lastAssistantText, "", "text while tools active should be blocked");

  // Second tool completes
  t.update({ type: "tool", content: [{ tool_use_id: "t2", content: "b", is_error: false }] });

  // Now text should be accepted
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Here are both results." }] } });
  assert.equal(finalMessage(t), "Here are both results.");
});

// ═══════════════════════════════════════════════════════════════════════════
// 5. Handoff (Amp hands off to a new thread)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Handoff ──");

test("handoff clears lastAssistantText and resultText", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "I've handed off to a new thread." }] } });
  assert.equal(t.lastAssistantText, "I've handed off to a new thread.");

  t.addHandoff("Continue the investigation", "new-thread-key");
  assert.equal(t.lastAssistantText, "");
  assert.equal(t.resultText, "");
  assert.equal(finalMessage(t), "");
});

test("handoff then new text from follow thread is captured", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Handing off..." }] } });
  t.addHandoff("Do the thing", "new-key");

  // Reconnected stream yields new text
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Finished the investigation." }] } });
  assert.equal(finalMessage(t), "Finished the investigation.");
});

// ═══════════════════════════════════════════════════════════════════════════
// 6. Task tool (Amp's sub-agent via the Task tool)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Task (sub-agent) tool ──");

test("preamble before Task tool_use is cleared", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me delegate this to a sub-agent." }] } });
  t.update({
    type: "assistant",
    message: {
      content: [{ type: "tool_use", id: "task-1", name: "Task", input: { prompt: "Fix the bug", description: "Fix type error" } }],
    },
  });
  assert.equal(t.lastAssistantText, "", "preamble cleared");

  // Stream dies mid-task
  assert.equal(finalMessage(t), "", "no bogus message posted");
});

test("text after Task completes is final answer", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "task-1", name: "Task", input: { prompt: "Fix it" } }] } });
  t.update({ type: "tool", content: [{ tool_use_id: "task-1", content: "Fixed 3 files", is_error: false }] });
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "The bug has been fixed." }] } });
  assert.equal(finalMessage(t), "The bug has been fixed.");
});

// ═══════════════════════════════════════════════════════════════════════════
// 7. Subagent events (Amp's system-level subagent tracking)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Subagent events ──");

test("subagent events produce task_update chunks but don't affect lastAssistantText", () => {
  const t = new ProgressTracker();
  t.update({ type: "subagent", status: "started", subagent_id: "sa-1", name: "Research task" });
  const chunks = t.pendingChunks();
  // init + subagent start (slot-based id)
  assert.ok(chunks.some((c) => c.type === "task_update" && c.id === "step-0" && c.status === "in_progress"));
  assert.equal(t.lastAssistantText, "", "subagent start doesn't set text");

  t.update({ type: "subagent", status: "completed", subagent_id: "sa-1", name: "Research task", summary: "Found 5 results" });
  const chunks2 = t.pendingChunks();
  assert.ok(chunks2.some((c) => c.type === "task_update" && c.id === "step-0" && c.status === "complete"));
  assert.equal(t.lastAssistantText, "", "subagent complete doesn't set text");
});

// ═══════════════════════════════════════════════════════════════════════════
// 8. Result event (explicit turn.done result)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Result event ──");

test("result event takes priority over lastAssistantText", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Intermediate text." }] } });
  t.update({ type: "result", text: "Final result from turn.done" });
  assert.equal(t.resultText, "Final result from turn.done");
  assert.equal(finalMessage(t), "Final result from turn.done");
});

test("result event is preferred even when lastAssistantText is set later", () => {
  const t = new ProgressTracker();
  t.update({ type: "result", text: "The real answer" });
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Some trailing text" }] } });
  // resultText takes priority in finalMessage()
  assert.equal(finalMessage(t), "The real answer");
});

// ═══════════════════════════════════════════════════════════════════════════
// 9. Error events
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Error events ──");

test("error event produces markdown_text chunk", () => {
  const t = new ProgressTracker();
  t.update({ type: "error", error: "Sandbox OOM killed" });
  const chunks = t.pendingChunks();
  assert.ok(chunks.some((c) => c.type === "markdown_text" && (c as any).text.includes("Sandbox OOM")));
});

// ═══════════════════════════════════════════════════════════════════════════
// 10. Reasoning events (should be no-ops)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Reasoning events ──");

test("reasoning event does not affect lastAssistantText", () => {
  const t = new ProgressTracker();
  t.update({ type: "reasoning", text: "I need to think about this carefully..." });
  assert.equal(t.lastAssistantText, "");
  assert.equal(finalMessage(t), "");
});

// ═══════════════════════════════════════════════════════════════════════════
// 11. Realistic full Amp turn: reason → preamble → tool → result → answer
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Realistic full Amp turn ──");

test("full turn: reasoning → preamble → Read → edit_file → final text", () => {
  const t = new ProgressTracker();

  // Extended thinking
  t.update({ type: "reasoning", text: "The user wants me to fix the bug in auth.ts" });

  // Preamble text
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "I'll fix the authentication bug." }] } });
  assert.equal(t.lastAssistantText, "I'll fix the authentication bug.");

  // Read file
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "r1", name: "Read", input: { path: "/src/auth.ts" } }] } });
  assert.equal(t.lastAssistantText, "", "cleared by tool_use");

  t.update({ type: "tool", content: [{ tool_use_id: "r1", content: "export function login() {...}", is_error: false }] });

  // Edit file
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "e1", name: "edit_file", input: { path: "/src/auth.ts", old_str: "old", new_str: "new" } }] } });
  t.update({ type: "tool", content: [{ tool_use_id: "e1", content: "ok", is_error: false }] });

  // Final answer
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Fixed the auth bug by updating the token validation." }] } });
  assert.equal(finalMessage(t), "Fixed the auth bug by updating the token validation.");
});

// ═══════════════════════════════════════════════════════════════════════════
// 12. Stream death scenarios
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Stream death scenarios ──");

test("stream dies after preamble + tool_use (no tool result) → empty", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me look at the Slack thread..." }] } });
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "read_thread", input: {} }] } });
  // EOF
  assert.equal(finalMessage(t), "");
});

test("stream dies after tool completes but before assistant text → empty", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/x" } }] } });
  t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "data", is_error: false }] });
  // EOF — no final text
  assert.equal(finalMessage(t), "");
});

test("stream dies mid second tool — preamble from first answer cycle is gone", () => {
  const t = new ProgressTracker();

  // First cycle completes fine
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: {} }] } });
  t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "ok", is_error: false }] });
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "Got the data, now editing..." }] } });
  assert.equal(t.lastAssistantText, "Got the data, now editing...");

  // Second tool starts — clears the intermediate text
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t2", name: "edit_file", input: {} }] } });
  assert.equal(t.lastAssistantText, "");

  // EOF — stream dies mid-edit
  assert.equal(finalMessage(t), "");
});

// ═══════════════════════════════════════════════════════════════════════════
// 13. Tool error doesn't break final text capture
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Tool errors ──");

test("tool error followed by final text works correctly", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t1", name: "Bash", input: { cmd: "make" } }] } });
  t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "exit code 1", is_error: true }] });
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "The build failed. Here's the error..." }] } });
  assert.equal(finalMessage(t), "The build failed. Here's the error...");
});

// ═══════════════════════════════════════════════════════════════════════════
// 14. Handoff tool call (Amp calls the handoff tool)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Handoff tool call ──");

test("handoff tool_use clears preamble like any other tool", () => {
  const t = new ProgressTracker();
  t.update({ type: "assistant", message: { content: [{ type: "text", text: "I'll hand this off to a new thread." }] } });
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "h1", name: "handoff", input: { goal: "Continue research", follow: true } }] } });
  assert.equal(t.lastAssistantText, "", "handoff tool_use clears text");
  assert.equal(finalMessage(t), "");
});

// ═══════════════════════════════════════════════════════════════════════════
// 15. Waterfall sliding window (max 5 visible steps, shift-up)
// ═══════════════════════════════════════════════════════════════════════════

console.log("\n── Waterfall sliding window ──");

test("first 5 tools each get a unique slot", () => {
  const t = new ProgressTracker();
  const starts: StreamChunk[] = [];

  for (let i = 0; i < 5; i++) {
    t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: `t${i}`, name: "Bash", input: { cmd: `echo ${i}` } }] } });
    const chunks = t.pendingChunks();
    const toolChunks = chunks.filter((c) => c.type === "task_update" && (c as any).id !== "init");
    starts.push(...toolChunks);
    t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] });
    t.pendingChunks(); // drain completions
  }

  const ids = starts.map((c) => (c as any).id);
  assert.deepEqual(ids, ["step-0", "step-1", "step-2", "step-3", "step-4"]);
});

test("6th tool shifts window: slots show tools 2-6 instead of 1-5", () => {
  const t = new ProgressTracker();

  // Complete 5 tools
  for (let i = 0; i < 5; i++) {
    t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: `t${i}`, name: "Read", input: { path: `/file${i}` } }] } });
    t.pendingChunks();
    t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] });
    t.pendingChunks();
  }

  // Start 6th tool — should trigger a shift
  t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: "t5", name: "Read", input: { path: "/file5" } }] } });
  const shiftChunks = t.pendingChunks();

  // Should re-emit all 5 slots with shifted content:
  // step-0=t1✓, step-1=t2✓, step-2=t3✓, step-3=t4✓, step-4=t5⏳
  const taskUpdates = shiftChunks.filter((c) => c.type === "task_update" && (c as any).id !== "init");
  assert.equal(taskUpdates.length, 5, "full window re-emitted on shift");

  // step-4 (newest) should be in_progress
  const last = taskUpdates.find((c) => (c as any).id === "step-4");
  assert.equal((last as any).status, "in_progress", "newest slot is in_progress");

  // step-0 (shifted) should be complete (was t1)
  const first = taskUpdates.find((c) => (c as any).id === "step-0");
  assert.equal((first as any).status, "complete", "oldest visible slot is complete");
});

test("all slot IDs stay within step-0 to step-4 regardless of tool count", () => {
  const t = new ProgressTracker();
  const allChunks: StreamChunk[] = [];

  for (let i = 0; i < 10; i++) {
    t.update({ type: "assistant", message: { content: [{ type: "tool_use", id: `t${i}`, name: "Read", input: { path: `/f${i}` } }] } });
    allChunks.push(...t.pendingChunks());
    t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] });
    allChunks.push(...t.pendingChunks());
  }

  const taskUpdates = allChunks.filter((c) => c.type === "task_update" && (c as any).id !== "init");
  const ids = new Set(taskUpdates.map((c) => (c as any).id));
  assert.deepEqual(ids, new Set(["step-0", "step-1", "step-2", "step-3", "step-4"]));
});

// ═══════════════════════════════════════════════════════════════════════════
// Summary
// ═══════════════════════════════════════════════════════════════════════════

console.log(`\n${"═".repeat(50)}`);
console.log(`Results: ${passed} passed, ${failed} failed`);
if (failed > 0) process.exit(1);
console.log("All tests passed ✓\n");
