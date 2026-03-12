import { describe, it, expect } from "vitest";
import fs from "node:fs";
import path from "node:path";

import { ProgressTracker } from "../src/lib/bot/progress-tracker";
import { HandoffDetector } from "../src/lib/bot/handoff-detection";
import { extractRunOptions } from "../src/lib/bot/harness";
import { normalizeHarnessEvent, type CanonicalEvent } from "@centaur/harness-events";

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function finalMessage(t: ProgressTracker): string {
  return (t.resultText || t.lastAssistantText).trim();
}

// Mirror of the annotation logic in bot.ts handleMessage — kept in sync.
function buildAnnotations(
  attachments: Array<{ url?: string; name?: string; mimeType?: string }>,
): string {
  const valid = attachments.filter(
    (a): a is { url: string; name: string; mimeType?: string } => !!a.url && !!a.name,
  );
  if (valid.length === 0) return "";
  return valid
    .map((a) => {
      const p = `/home/agent/uploads/${a.name}`;
      return a.mimeType?.startsWith("image/")
        ? `[Attached image: ${p}]`
        : `[Attached file: ${p}]`;
    })
    .join("\n");
}

function parseSSEFile(filePath: string): Record<string, unknown>[] {
  const raw = fs.readFileSync(filePath, "utf-8");
  const events: Record<string, unknown>[] = [];
  for (const line of raw.split("\n")) {
    const trimmed = line.trim();
    if (!trimmed.startsWith("data: ")) continue;
    const payload = trimmed.slice(6);
    if (payload === "[DONE]") continue;
    try {
      events.push(JSON.parse(payload));
    } catch {
      // skip
    }
  }
  return events;
}

function replayFixture(name: string) {
  const filePath = path.join(
    __dirname,
    "../src/lib/bot/fixtures",
    `${name}.sse`,
  );
  const rawEvents = parseSSEFile(filePath);
  const tracker = new ProgressTracker();
  const allCanonical: CanonicalEvent[] = [];
  const allChunks: unknown[] = [];
  let turnDoneResult = "";

  for (const raw of rawEvents) {
    if (raw.type === "turn.done") {
      turnDoneResult = typeof raw.result === "string" ? raw.result : "";
    }
    const canonical = normalizeHarnessEvent("amp", raw);
    for (const ce of canonical) {
      allCanonical.push(ce);
      if (tracker.update(ce)) {
        allChunks.push(...tracker.pendingChunks());
      }
    }
  }

  return { tracker, allCanonical, allChunks, rawEvents, turnDoneResult };
}

// ═══════════════════════════════════════════════════════════════════════════════
// 1. Attachment annotations
// ═══════════════════════════════════════════════════════════════════════════════

describe("attachment annotations", () => {
  it("image mimeType → [Attached image: ...]", () => {
    expect(
      buildAnnotations([
        { url: "https://files.slack.com/a", name: "screenshot.png", mimeType: "image/png" },
      ]),
    ).toBe("[Attached image: /home/agent/uploads/screenshot.png]");
  });

  it("non-image mimeType → [Attached file: ...]", () => {
    expect(
      buildAnnotations([
        { url: "https://files.slack.com/a", name: "report.pdf", mimeType: "application/pdf" },
      ]),
    ).toBe("[Attached file: /home/agent/uploads/report.pdf]");
  });

  it("undefined mimeType → [Attached file: ...]", () => {
    expect(
      buildAnnotations([{ url: "https://files.slack.com/a", name: "data.csv" }]),
    ).toBe("[Attached file: /home/agent/uploads/data.csv]");
  });

  it("mixed image and non-image attachments", () => {
    expect(
      buildAnnotations([
        { url: "https://x/1", name: "photo.jpg", mimeType: "image/jpeg" },
        { url: "https://x/2", name: "doc.xlsx", mimeType: "application/vnd.ms-excel" },
        { url: "https://x/3", name: "chart.gif", mimeType: "image/gif" },
      ]),
    ).toBe(
      "[Attached image: /home/agent/uploads/photo.jpg]\n" +
        "[Attached file: /home/agent/uploads/doc.xlsx]\n" +
        "[Attached image: /home/agent/uploads/chart.gif]",
    );
  });

  it("filters out attachments with missing url or name", () => {
    expect(
      buildAnnotations([
        { url: "", name: "no-url.png", mimeType: "image/png" },
        { url: "https://x/b", name: "" },
        { url: "https://x/c", name: "good.txt", mimeType: "text/plain" },
      ]),
    ).toBe("[Attached file: /home/agent/uploads/good.txt]");
  });

  it("returns empty string for empty array", () => {
    expect(buildAnnotations([])).toBe("");
  });

  it("image/svg+xml counts as image", () => {
    expect(
      buildAnnotations([
        { url: "https://x/s", name: "logo.svg", mimeType: "image/svg+xml" },
      ]),
    ).toBe("[Attached image: /home/agent/uploads/logo.svg]");
  });

  it("video and audio are files, not images", () => {
    expect(
      buildAnnotations([
        { url: "https://x/v", name: "clip.mp4", mimeType: "video/mp4" },
        { url: "https://x/a", name: "voice.ogg", mimeType: "audio/ogg" },
      ]),
    ).toBe(
      "[Attached file: /home/agent/uploads/clip.mp4]\n" +
        "[Attached file: /home/agent/uploads/voice.ogg]",
    );
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 2. extractRunOptions
// ═══════════════════════════════════════════════════════════════════════════════

describe("extractRunOptions", () => {
  it("defaults to amp with no flags", () => {
    const r = extractRunOptions("build me a dashboard");
    expect(r.harness).toBe("amp");
    expect(r.cleanedText).toBe("build me a dashboard");
    expect(r.harnessExplicit).toBe(false);
    expect(r.budgetMode).toBeNull();
  });

  it("parses --claude flag", () => {
    const r = extractRunOptions("--claude analyze this");
    expect(r.harness).toBe("claude-code");
    expect(r.harnessExplicit).toBe(true);
    expect(r.cleanedText).toBe("analyze this");
  });

  it("parses --amp flag", () => {
    const r = extractRunOptions("--amp fix the bug");
    expect(r.harness).toBe("amp");
    expect(r.harnessExplicit).toBe(true);
  });

  it("parses harness=claude-code key-value", () => {
    const r = extractRunOptions("harness=claude-code do something");
    expect(r.harness).toBe("claude-code");
    expect(r.harnessExplicit).toBe(true);
    expect(r.cleanedText).toBe("do something");
  });

  it("parses --simple budget mode", () => {
    const r = extractRunOptions("--simple quick question");
    expect(r.budgetMode).toBe("simple");
    expect(r.cleanedText).toBe("quick question");
  });

  it("parses --deep budget mode", () => {
    const r = extractRunOptions("--deep investigate the crash");
    expect(r.budgetMode).toBe("complex");
    expect(r.cleanedText).toBe("investigate the crash");
  });

  it("parses mode=auto key-value", () => {
    const r = extractRunOptions("mode=auto do it");
    expect(r.budgetMode).toBe("auto");
    expect(r.cleanedText).toBe("do it");
  });

  it("combines harness and budget flags", () => {
    const r = extractRunOptions("--claude --complex run a deep analysis");
    expect(r.harness).toBe("claude-code");
    expect(r.budgetMode).toBe("complex");
    expect(r.cleanedText).toBe("run a deep analysis");
  });

  it("strips legacy --opus/--sonnet/--haiku flags", () => {
    const r = extractRunOptions("--opus tell me about ETH");
    expect(r.cleanedText).toBe("tell me about ETH");
    expect(r.harness).toBe("amp");
  });

  it("treats unknown --flag as persona name", () => {
    const r = extractRunOptions("--legal review this contract");
    expect(r.harness).toBe("legal");
    expect(r.harnessExplicit).toBe(true);
    expect(r.cleanedText).toBe("review this contract");
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 3. ProgressTracker
// ═══════════════════════════════════════════════════════════════════════════════

describe("ProgressTracker", () => {
  it("captures text-only assistant message as finalMessage", () => {
    const t = new ProgressTracker();
    t.update({
      type: "assistant",
      message: { content: [{ type: "text", text: "Here is your answer." }] },
    });
    expect(finalMessage(t)).toBe("Here is your answer.");
  });

  it("last text event wins", () => {
    const t = new ProgressTracker();
    t.update({ type: "assistant", message: { content: [{ type: "text", text: "First." }] } });
    t.update({ type: "assistant", message: { content: [{ type: "text", text: "Second." }] } });
    expect(finalMessage(t)).toBe("Second.");
  });

  it("clears preamble when tool_use starts (separate events)", () => {
    const t = new ProgressTracker();
    t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me look..." }] } });
    t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/x" } }] },
    });
    expect(finalMessage(t)).toBe("");
  });

  it("clears preamble when tool_use starts (same event)", () => {
    const t = new ProgressTracker();
    t.update({
      type: "assistant",
      message: {
        content: [
          { type: "text", text: "Let me search..." },
          { type: "tool_use", id: "t1", name: "finder", input: { query: "auth" } },
        ],
      },
    });
    expect(finalMessage(t)).toBe("");
  });

  it("captures final text after tool completes", () => {
    const t = new ProgressTracker();
    t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: { path: "/x" } }] },
    });
    t.update({ type: "tool", content: [{ tool_use_id: "t1", content: "data", is_error: false }] });
    t.update({
      type: "assistant",
      message: { content: [{ type: "text", text: "Done fixing the bug." }] },
    });
    expect(finalMessage(t)).toBe("Done fixing the bug.");
  });

  it("result event takes priority over lastAssistantText", () => {
    const t = new ProgressTracker();
    t.update({ type: "assistant", message: { content: [{ type: "text", text: "Intermediate." }] } });
    t.update({ type: "result", text: "Final from turn.done" });
    expect(finalMessage(t)).toBe("Final from turn.done");
  });

  it("stream death after tool_use → empty finalMessage", () => {
    const t = new ProgressTracker();
    t.update({ type: "assistant", message: { content: [{ type: "text", text: "Let me check..." }] } });
    t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t1", name: "Read", input: {} }] },
    });
    expect(finalMessage(t)).toBe("");
  });

  it("error event produces markdown_text chunk", () => {
    const t = new ProgressTracker();
    t.update({ type: "error", error: "OOM killed" });
    const chunks = t.pendingChunks();
    expect(chunks.some((c) => c.type === "markdown_text" && "text" in c && (c as any).text.includes("OOM"))).toBe(true);
  });

  it("reasoning event does not affect lastAssistantText", () => {
    const t = new ProgressTracker();
    t.update({ type: "reasoning", text: "Thinking hard..." });
    expect(finalMessage(t)).toBe("");
  });

  it("first 5 tools each get a unique slot", () => {
    const t = new ProgressTracker();
    const starts: unknown[] = [];
    for (let i = 0; i < 5; i++) {
      t.update({
        type: "assistant",
        message: { content: [{ type: "tool_use", id: `t${i}`, name: "Bash", input: { cmd: `echo ${i}` } }] },
      });
      const chunks = t.pendingChunks();
      starts.push(...chunks.filter((c) => c.type === "task_update" && (c as any).id !== "init"));
      t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] });
      t.pendingChunks();
    }
    const ids = (starts as any[]).map((c) => c.id);
    expect(ids).toEqual(["step-0", "step-1", "step-2", "step-3", "step-4"]);
  });

  it("6th tool shifts window up — slots show tools 2-6", () => {
    const t = new ProgressTracker();
    for (let i = 0; i < 5; i++) {
      t.update({
        type: "assistant",
        message: { content: [{ type: "tool_use", id: `t${i}`, name: "Read", input: { path: `/file${i}` } }] },
      });
      t.pendingChunks();
      t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] });
      t.pendingChunks();
    }
    // 6th tool triggers a shift
    t.update({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "t5", name: "Read", input: { path: "/file5" } }] },
    });
    const shiftChunks = t.pendingChunks();
    const taskUpdates = shiftChunks.filter((c) => c.type === "task_update" && (c as any).id !== "init");
    // Full window re-emitted
    expect(taskUpdates).toHaveLength(5);
    // Newest slot (step-4) is in_progress
    expect(taskUpdates.find((c) => (c as any).id === "step-4")).toMatchObject({ status: "in_progress" });
    // Oldest visible (step-0) is complete (was t1, not t0)
    expect(taskUpdates.find((c) => (c as any).id === "step-0")).toMatchObject({ status: "complete" });
  });

  it("all slot IDs stay within step-0..step-4 regardless of tool count", () => {
    const t = new ProgressTracker();
    const allChunks: unknown[] = [];
    for (let i = 0; i < 10; i++) {
      t.update({
        type: "assistant",
        message: { content: [{ type: "tool_use", id: `t${i}`, name: "Read", input: { path: `/f${i}` } }] },
      });
      allChunks.push(...t.pendingChunks());
      t.update({ type: "tool", content: [{ tool_use_id: `t${i}`, content: "ok", is_error: false }] });
      allChunks.push(...t.pendingChunks());
    }
    const ids = new Set(
      (allChunks as any[])
        .filter((c) => c.type === "task_update" && c.id !== "init")
        .map((c) => c.id),
    );
    expect(ids).toEqual(new Set(["step-0", "step-1", "step-2", "step-3", "step-4"]));
  });

  it("subagent events produce task_update chunks", () => {
    const t = new ProgressTracker();
    t.update({ type: "subagent", status: "started", subagent_id: "sa-1", name: "Research" });
    const chunks = t.pendingChunks();
    expect(chunks.some((c) => c.type === "task_update" && (c as any).status === "in_progress")).toBe(true);
    expect(t.lastAssistantText).toBe("");
  });

  it("addHandoff clears state and produces task_update", () => {
    const t = new ProgressTracker();
    t.update({ type: "assistant", message: { content: [{ type: "text", text: "intermediate" }] } });
    t.addHandoff("Continue research", "T-new-123");
    expect(t.lastAssistantText).toBe("");
    expect(t.resultText).toBe("");
    const chunks = t.pendingChunks();
    expect(chunks.some((c) => c.type === "task_update" && (c as any).title.includes("Continue research"))).toBe(true);
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 4. HandoffDetector
// ═══════════════════════════════════════════════════════════════════════════════

describe("HandoffDetector", () => {
  it("detects follow=true handoff from tool_use + tool_result pair", () => {
    const d = new HandoffDetector();
    expect(
      d.processEvent({
        type: "assistant",
        message: {
          content: [
            {
              type: "tool_use",
              id: "h1",
              name: "handoff",
              input: { goal: "Continue work", follow: true },
            },
          ],
        },
      }),
    ).toBeNull();

    const result = d.processEvent({
      type: "tool",
      content: [
        {
          tool_use_id: "h1",
          content: JSON.stringify({ newThreadID: "T-abc-123" }),
          is_error: false,
        },
      ],
    });
    expect(result).not.toBeNull();
    expect(result!.newThreadKey).toBe("T-abc-123");
    expect(result!.follow).toBe(true);
    expect(result!.goal).toBe("Continue work");
  });

  it("ignores handoff with follow=false", () => {
    const d = new HandoffDetector();
    d.processEvent({
      type: "assistant",
      message: {
        content: [
          { type: "tool_use", id: "h1", name: "handoff", input: { goal: "bg task", follow: false } },
        ],
      },
    });
    const result = d.processEvent({
      type: "tool",
      content: [
        { tool_use_id: "h1", content: JSON.stringify({ newThreadID: "T-xyz" }), is_error: false },
      ],
    });
    expect(result).toBeNull();
  });

  it("ignores non-handoff tool calls", () => {
    const d = new HandoffDetector();
    d.processEvent({
      type: "assistant",
      message: { content: [{ type: "tool_use", id: "r1", name: "Read", input: { path: "/x" } }] },
    });
    const result = d.processEvent({
      type: "tool",
      content: [{ tool_use_id: "r1", content: "file content", is_error: false }],
    });
    expect(result).toBeNull();
  });
});

// ═══════════════════════════════════════════════════════════════════════════════
// 5. SSE fixture replay
// ═══════════════════════════════════════════════════════════════════════════════

const fixtureDir = path.join(__dirname, "../src/lib/bot/fixtures");
const fixtureFiles = fs.readdirSync(fixtureDir).filter((f) => f.endsWith(".sse"));

describe("SSE fixture replay", () => {
  for (const file of fixtureFiles) {
    const name = file.replace(".sse", "");

    describe(name, () => {
      const { tracker, allCanonical, allChunks, rawEvents, turnDoneResult } =
        replayFixture(name);
      const fm = finalMessage(tracker);

      it("parses raw SSE events", () => {
        expect(rawEvents.length).toBeGreaterThan(0);
      });

      it("produces canonical events", () => {
        expect(allCanonical.length).toBeGreaterThan(0);
      });

      const toolUseEvents = allCanonical.filter(
        (e) =>
          e.type === "assistant" &&
          e.message?.content.some((b: { type: string }) => b.type === "tool_use"),
      );

      if (toolUseEvents.length > 0) {
        it("tool_use events produce task_update chunks", () => {
          const taskUpdates = (allChunks as any[]).filter(
            (c) => c.type === "task_update" && c.id !== "init",
          );
          expect(taskUpdates.length).toBeGreaterThan(0);
        });
      }

      if (turnDoneResult) {
        it("finalMessage matches turn.done result", () => {
          expect(fm).toBe(turnDoneResult);
        });

        it("no dangling active tools at end", () => {
          const activeTools = (tracker as any).activeTools as Map<string, unknown>;
          expect(activeTools.size).toBe(0);
        });
      }

      if (name !== "handoff" && turnDoneResult) {
        it("finalMessage is non-empty", () => {
          expect(fm.length).toBeGreaterThan(0);
        });
      }

      if (toolUseEvents.length > 0 && fm) {
        it("finalMessage is NOT preamble text", () => {
          let firstTextBeforeTool = "";
          let seenToolUse = false;
          for (const ce of allCanonical) {
            if (ce.type === "assistant" && ce.message?.content) {
              for (const block of ce.message.content) {
                if (block.type === "text" && block.text && !seenToolUse) {
                  firstTextBeforeTool = block.text;
                }
                if (block.type === "tool_use") {
                  seenToolUse = true;
                }
              }
            }
          }
          if (firstTextBeforeTool && firstTextBeforeTool !== turnDoneResult) {
            expect(fm).not.toBe(firstTextBeforeTool);
          }
        });
      }
    });
  }
});

// ═══════════════════════════════════════════════════════════════════════════════
// 6. Simulated stream deaths
// ═══════════════════════════════════════════════════════════════════════════════

describe("simulated stream deaths", () => {
  for (const file of fixtureFiles) {
    const name = file.replace(".sse", "");
    const filePath = path.join(fixtureDir, file);
    const rawEvents = parseSSEFile(filePath);

    // Find first event index that contains tool_use
    let firstToolUseIdx = -1;
    for (let i = 0; i < rawEvents.length; i++) {
      const canonical = normalizeHarnessEvent("amp", rawEvents[i]);
      for (const ce of canonical) {
        if (
          ce.type === "assistant" &&
          ce.message?.content.some((b: { type: string }) => b.type === "tool_use")
        ) {
          firstToolUseIdx = i;
          break;
        }
      }
      if (firstToolUseIdx >= 0) break;
    }

    if (firstToolUseIdx < 0) continue;

    it(`${name}: EOF after first tool_use → empty finalMessage`, () => {
      const tracker = new ProgressTracker();
      for (let i = 0; i <= firstToolUseIdx; i++) {
        const canonical = normalizeHarnessEvent("amp", rawEvents[i]);
        for (const ce of canonical) {
          tracker.update(ce);
          tracker.pendingChunks();
        }
      }
      expect(finalMessage(tracker)).toBe("");
    });
  }
});
