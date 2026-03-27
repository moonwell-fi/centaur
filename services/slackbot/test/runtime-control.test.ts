import { describe, expect, it, vi } from "vitest";

import type { StreamChunk } from "chat";

import { SlackBot, type BotMessage, type BotThread, type SlackAdapter } from "../src/lib/bot/bot";

function createThread(id = "slack:C123:1700000000.000100") {
  const postedMarkdown: string[] = [];
  const streamedChunks: StreamChunk[] = [];
  let postCount = 0;

  const thread: BotThread = {
    id,
    async subscribe() {},
    async startTyping() {},
    async post(content) {
      postCount += 1;
      if ("markdown" in content) {
        postedMarkdown.push(content.markdown);
      } else {
        for await (const chunk of content) {
          streamedChunks.push(chunk);
        }
      }
      return {
        id: `msg-${postedMarkdown.length + streamedChunks.length}`,
        async edit(c: { markdown: string }) {
          postedMarkdown.push(c.markdown);
        },
      };
    },
  };

  return {
    thread,
    postedMarkdown,
    streamedChunks,
    get postCount() {
      return postCount;
    },
  };
}

function userMessage(text: string, opts?: { id?: string; teamId?: string; isMention?: boolean }): BotMessage {
  const ts = opts?.id || "1700000000.000100";
  return {
    id: ts,
    text,
    isMention: opts?.isMention,
    raw: {
      ts,
      team_id: opts?.teamId || "T123",
    },
    author: {
      isMe: false,
      isBot: false,
      userId: "U123",
    },
  };
}

function createImmediateStreamClient(): any {
  return {
    spawn: vi.fn(async () => ({ assignment_generation: 7 })),
    message: vi.fn(async () => ({ ok: true, attachment_ids: [] })),
    execute: vi.fn(async () => ({ execution_id: "exe-new" })),
    streamEvents: vi.fn(() => (async function* () {
      yield {
        eventId: 1,
        eventKind: "amp_raw_event",
        data: {
          type: "turn.done",
          result: "done",
        },
      };
    })()),
    cancelExecution: vi.fn(async () => ({ ok: true })),
    markFinalDelivered: vi.fn(async () => ({ ok: true })),
    markFinalFailed: vi.fn(async () => ({ ok: true })),
    claimFinalDeliveries: vi.fn(async () => ({ deliveries: [] })),
    getExecution: vi.fn(async () => ({ status: "completed", result_text: "done" })),
  };
}

function createSlackAdapter(overrides?: Partial<SlackAdapter>): SlackAdapter {
  return {
    fetchMessage: async () => null,
    fetchMessages: async () => ({ messages: [] }),
    postMessage: async () => ({ id: "msg-1" }),
    setAssistantTitle: async () => {},
    getInstallation: async () => null,
    withBotToken: async (_token, fn) => await fn(),
    ...overrides,
  };
}

describe("SlackBot runtime control", () => {
  const normalizedThreadKey = "C123:1700000000.000100";

  it("uses stable Slack message IDs for history backfill and the current message", async () => {
    const client = createImmediateStreamClient();
    const slack = createSlackAdapter({
      fetchMessages: async () => ({
        messages: [
          userMessage("prior context", { id: "1700000000.000001" }) as any,
          userMessage("<@bot> please help", { id: "1700000000.000002" }) as any,
        ],
      }),
    });
    const bot = new SlackBot(client as any, "", slack);
    const { thread } = createThread();

    await bot.onNewMention(thread, userMessage("<@bot> please help", { id: "1700000000.000002" }));

    expect(client.message).toHaveBeenCalledTimes(2);
    expect(client.message.mock.calls[0][0].messageId).toBe("slack:1700000000.000001");
    expect(client.message.mock.calls[1][0].messageId).toBe("slack:1700000000.000002");
  });

  it("cancels the previous execution before starting a new mention turn", async () => {
    const client = createImmediateStreamClient();
    const bot = new SlackBot(client as any);
    const { thread } = createThread();
    const oldAbortController = new AbortController();

    (bot as any).inFlightExecutions.set(normalizedThreadKey, {
      executionId: "exe-old",
      abortController: oldAbortController,
    });

    await bot.onSubscribedMessage(thread, userMessage("follow-up", {
      id: "1700000000.000003",
      isMention: true,
    }));

    expect(oldAbortController.signal.aborted).toBe(true);
    expect(client.cancelExecution).toHaveBeenCalledWith("exe-old");
  });

  it("does not leave a blank streamed message behind when an execution is interrupted before text", async () => {
    let nextExecution = 1;
    const client = {
      spawn: vi.fn(async () => ({ assignment_generation: 7 })),
      message: vi.fn(async () => ({ ok: true, attachment_ids: [] })),
      execute: vi.fn(async () => ({ execution_id: `exe-${nextExecution++}` })),
      streamEvents: vi.fn(({ executionId, signal }: { executionId: string; signal?: AbortSignal }) => {
        if (executionId === "exe-1") {
          return (async function* () {
            await new Promise<void>((resolve) => {
              if (signal?.aborted) {
                resolve();
                return;
              }
              signal?.addEventListener("abort", () => resolve(), { once: true });
            });
          })();
        }

        return (async function* () {
          yield {
            eventId: 1,
            eventKind: "amp_raw_event",
            data: {
              type: "turn.done",
              result: "done",
            },
          };
        })();
      }),
      cancelExecution: vi.fn(async () => ({ ok: true })),
      markFinalDelivered: vi.fn(async () => ({ ok: true })),
      markFinalFailed: vi.fn(async () => ({ ok: true })),
      claimFinalDeliveries: vi.fn(async () => ({ deliveries: [] })),
      getExecution: vi.fn(async () => ({ status: "completed", result_text: "done" })),
    };
    const bot = new SlackBot(client as any);
    const runtime = createThread();
    const { thread, streamedChunks } = runtime;

    const firstTurn = bot.onSubscribedMessage(thread, userMessage("first", {
      id: "1700000000.000003",
      isMention: true,
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));

    await bot.onSubscribedMessage(thread, userMessage("second", {
      id: "1700000000.000004",
      isMention: true,
    }));
    await firstTurn;

    expect(client.cancelExecution).toHaveBeenCalledWith("exe-1");
    expect(runtime.postCount).toBe(1);
    expect(streamedChunks.some((chunk) => chunk.type === "markdown_text")).toBe(true);
  });

  it("acks live streamed deliveries without requiring an outbox lease", async () => {
    const client = createImmediateStreamClient();
    const bot = new SlackBot(client as any);
    const { thread } = createThread();

    await bot.onSubscribedMessage(thread, userMessage("follow-up", {
      id: "1700000000.000004",
      isMention: true,
    }));

    expect(client.markFinalDelivered).toHaveBeenCalledWith("exe-new", undefined);
  });

  it("uses the stored terminal result when the event stream reconnects after completion", async () => {
    const client = createImmediateStreamClient();
    client.streamEvents = vi.fn(() => (async function* () {
      // API reconnect edge-case: no unseen events remain, so consumeExecutionEvents
      // has to fall back to the durable execution row.
    })());
    client.getExecution = vi.fn(async () => ({ status: "completed", result_text: "stored answer" }));

    const bot = new SlackBot(client as any);
    const runtime = createThread();

    await bot.onSubscribedMessage(runtime.thread, userMessage("follow-up", {
      id: "1700000000.000004",
      isMention: true,
    }));

    expect(client.getExecution).toHaveBeenCalledWith("exe-new");
    expect(
      runtime.streamedChunks.some(
        (chunk) => chunk.type === "markdown_text" && chunk.text.includes("stored answer"),
      ),
    ).toBe(true);
  });

  it("claims only Slack final deliveries and posts completed results once", async () => {
    const client = createImmediateStreamClient();
    client.claimFinalDeliveries = vi.fn(async () => ({
      deliveries: [
        {
          execution_id: "exe-completed",
          thread_key: normalizedThreadKey,
          delivery: { platform: "slack" },
          final_payload: { status: "completed", result_text: "final answer" },
        },
        {
          execution_id: "exe-cancelled",
          thread_key: normalizedThreadKey,
          delivery: { platform: "slack" },
          final_payload: { status: "cancelled", terminal_reason: "cancel_requested" },
        },
      ],
    }));
    const slack = createSlackAdapter({
      postMessage: vi.fn(async () => ({ id: "msg-final" })),
    });
    const bot = new SlackBot(client as any, "", slack);

    await (bot as any).drainFinalDeliveriesOnce();

    expect(client.claimFinalDeliveries).toHaveBeenCalledWith(expect.objectContaining({ platform: "slack" }));
    expect(slack.postMessage).toHaveBeenCalledTimes(1);
    expect(slack.postMessage).toHaveBeenCalledWith(
      `slack:${normalizedThreadKey}`,
      { markdown: "final answer" },
    );
    expect(client.markFinalDelivered).toHaveBeenCalledWith("exe-completed", expect.any(String));
    expect(client.markFinalDelivered).toHaveBeenCalledWith("exe-cancelled", expect.any(String));
  });

  it("defers outbox delivery while the same execution is still streaming locally", async () => {
    const client = createImmediateStreamClient();
    client.claimFinalDeliveries = vi.fn(async () => ({
      deliveries: [
        {
          execution_id: "exe-live",
          thread_key: normalizedThreadKey,
          delivery: { platform: "slack" },
          final_payload: { status: "completed", result_text: "should wait" },
        },
      ],
    }));
    const slack = createSlackAdapter({
      postMessage: vi.fn(async () => ({ id: "msg-final" })),
    });
    const bot = new SlackBot(client as any, "", slack);

    (bot as any).inFlightExecutions.set(normalizedThreadKey, {
      executionId: "exe-live",
      abortController: new AbortController(),
    });

    await (bot as any).drainFinalDeliveriesOnce();

    expect(slack.postMessage).not.toHaveBeenCalled();
    expect(client.markFinalFailed).not.toHaveBeenCalled();
    expect(client.markFinalDelivered).not.toHaveBeenCalled();
  });
});
