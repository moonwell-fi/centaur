import { describe, expect, it, vi } from "vitest";

import type { StreamChunk } from "../src/lib/slack/types";

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
    startWorkflowRun: vi.fn(async () => ({ execution_id: "exe-new", status: "waiting" })),
    execute: vi.fn(async () => ({ execution_id: "exe-new" })),
    releaseThread: vi.fn(async () => ({ ok: true, released: true })),
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
    renewFinalDeliveryLease: vi.fn(async () => ({ ok: true })),
    claimFinalDeliveries: vi.fn(async () => ({ deliveries: [] })),
    listExecutions: vi.fn(async (threadKey: string) => ({ thread_key: threadKey, executions: [] })),
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

    expect(client.message).toHaveBeenCalledTimes(1);
    expect(client.message.mock.calls[0][0].messageId).toBe("slack:1700000000.000001");
    expect(client.startWorkflowRun).toHaveBeenCalledTimes(1);
    expect(client.startWorkflowRun.mock.calls[0][0].triggerKey).toBe(
      `slack-thread-turn:${normalizedThreadKey}:slack:1700000000.000002`,
    );
  });

  it("releases the active assignment before an explicit persona switch", async () => {
    const client = createImmediateStreamClient();
    const bot = new SlackBot(client as any, "", createSlackAdapter());
    const { thread } = createThread();

    await bot.onSubscribedMessage(thread, userMessage("<@bot> dont u have --invest", {
      id: "1700000000.000300",
      isMention: true,
    }));

    expect(client.releaseThread).toHaveBeenCalledWith(normalizedThreadKey, {
      releaseId: "prompt-switch:slack:1700000000.000300",
      cancelInflight: true,
    });
    expect(client.startWorkflowRun.mock.calls[0][0].input.prompt_selector).toBe("invest");
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
      startWorkflowRun: vi.fn(async () => ({ execution_id: `exe-${nextExecution++}`, status: "waiting" })),
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

  it("maps failed-permanent hydration to a friendly retry message", async () => {
    const client = createImmediateStreamClient();
    client.streamEvents = vi.fn(() => (async function* () {
      // Force hydration from the durable execution row.
    })());
    client.getExecution = vi.fn(async () => ({
      status: "failed_permanent",
      terminal_reason: "harness_error",
      error_text: "Connection error.",
    }));

    const bot = new SlackBot(client as any);
    const runtime = createThread();

    await bot.onSubscribedMessage(runtime.thread, userMessage("follow-up", {
      id: "1700000000.000004",
      isMention: true,
    }));

    expect(
      runtime.streamedChunks.some(
        (chunk) => chunk.type === "markdown_text" && chunk.text.includes("Agent hit a runtime issue before finishing. Please retry."),
      ),
    ).toBe(true);
    expect(
      runtime.streamedChunks.some(
        (chunk) => chunk.type === "markdown_text" && chunk.text.includes("Connection error."),
      ),
    ).toBe(false);
  });

  it("maps cancelled hydration to a friendly cancellation message", async () => {
    const client = createImmediateStreamClient();
    client.streamEvents = vi.fn(() => (async function* () {
      // Force hydration from the durable execution row.
    })());
    client.getExecution = vi.fn(async () => ({
      status: "cancelled",
      terminal_reason: "cancel_requested",
      error_text: "cancel_requested",
    }));

    const bot = new SlackBot(client as any);
    const runtime = createThread();

    await bot.onSubscribedMessage(runtime.thread, userMessage("follow-up", {
      id: "1700000000.000004",
      isMention: true,
    }));

    expect(
      runtime.streamedChunks.some(
        (chunk) => chunk.type === "markdown_text" && chunk.text.includes("Request cancelled. Send another message when you want to retry."),
      ),
    ).toBe(true);
    expect(
      runtime.streamedChunks.some(
        (chunk) => chunk.type === "markdown_text" && chunk.text.includes("cancel_requested"),
      ),
    ).toBe(false);
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
    client.listExecutions = vi.fn(async () => ({
      thread_key: normalizedThreadKey,
      executions: [
        { execution_id: "exe-completed", status: "completed" },
        { execution_id: "exe-cancelled", status: "cancelled" },
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

  it("uses explicit Slack delivery destination for workflow final deliveries", async () => {
    const client = createImmediateStreamClient();
    client.claimFinalDeliveries = vi.fn(async () => ({
      deliveries: [
        {
          execution_id: "exe-workflow",
          thread_key: "workflow:wfr_123:notify",
          delivery: {
            platform: "slack",
            channel: "C999",
            thread_ts: "1700000000.999999",
          },
          final_payload: { status: "completed", result_text: "workflow result" },
        },
      ],
    }));
    const slack = createSlackAdapter({
      postMessage: vi.fn(async () => ({ id: "msg-final" })),
    });
    const bot = new SlackBot(client as any, "", slack);

    await (bot as any).drainFinalDeliveriesOnce();

    expect(slack.postMessage).toHaveBeenCalledWith(
      "slack:C999:1700000000.999999",
      { markdown: "workflow result" },
    );
    expect(client.markFinalDelivered).toHaveBeenCalledWith("exe-workflow", expect.any(String));
  });

  it("posts a friendly cancellation message for the latest cancelled final delivery", async () => {
    const client = createImmediateStreamClient();
    client.claimFinalDeliveries = vi.fn(async () => ({
      deliveries: [
        {
          execution_id: "exe-cancelled",
          thread_key: normalizedThreadKey,
          delivery: { platform: "slack" },
          final_payload: { status: "cancelled", terminal_reason: "cancel_requested", error_text: "cancel_requested" },
        },
      ],
    }));
    client.listExecutions = vi.fn(async () => ({
      thread_key: normalizedThreadKey,
      executions: [
        { execution_id: "exe-cancelled", status: "cancelled" },
      ],
    }));
    const slack = createSlackAdapter({
      postMessage: vi.fn(async () => ({ id: "msg-final" })),
    });
    const bot = new SlackBot(client as any, "", slack);

    await (bot as any).drainFinalDeliveriesOnce();

    expect(slack.postMessage).toHaveBeenCalledWith(
      `slack:${normalizedThreadKey}`,
      { markdown: "Request cancelled. Send another message when you want to retry." },
    );
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
    expect(client.renewFinalDeliveryLease).toHaveBeenCalledWith(
      "exe-live",
      expect.objectContaining({
        consumerId: expect.any(String),
        leaseSeconds: 90,
      }),
    );
  });

  it("retries final delivery with flattened tables when Slack rejects blocks", async () => {
    const client = createImmediateStreamClient();
    client.claimFinalDeliveries = vi.fn(async () => ({
      deliveries: [
        {
          execution_id: "exe-table",
          thread_key: normalizedThreadKey,
          delivery: { platform: "slack" },
          final_payload: {
            status: "completed",
            result_text: [
              "Summary",
              "",
              "| Asset | Value |",
              "| --- | --- |",
              "| BTC | $1.00M |",
            ].join("\n"),
          },
        },
      ],
    }));
    const postMessage = vi
      .fn()
      .mockRejectedValueOnce(new Error("invalid_blocks"))
      .mockResolvedValue({ id: "msg-safe" });
    const slack = createSlackAdapter({ postMessage });
    const bot = new SlackBot(client as any, "", slack);

    await (bot as any).drainFinalDeliveriesOnce();

    expect(postMessage).toHaveBeenCalledTimes(2);
    expect(postMessage.mock.calls[1][1]).toEqual({
      markdown: [
        "Summary",
        "",
        "- Asset: BTC; Value: $1.00M",
      ].join("\n"),
    });
    expect(client.markFinalDelivered).toHaveBeenCalledWith("exe-table", expect.any(String));
    expect(client.markFinalFailed).not.toHaveBeenCalled();
  });

  it("maps silence-deadline final delivery to a friendly retry message", async () => {
    const client = createImmediateStreamClient();
    client.claimFinalDeliveries = vi.fn(async () => ({
      deliveries: [
        {
          execution_id: "exe-silent",
          thread_key: normalizedThreadKey,
          delivery: { platform: "slack" },
          final_payload: {
            status: "failed_permanent",
            terminal_reason: "silence_deadline_exceeded",
            error_text: "execution made no progress before silence deadline",
          },
        },
      ],
    }));
    const slack = createSlackAdapter({
      postMessage: vi.fn(async () => ({ id: "msg-final" })),
    });
    const bot = new SlackBot(client as any, "", slack);

    await (bot as any).drainFinalDeliveriesOnce();

    expect(slack.postMessage).toHaveBeenCalledWith(
      `slack:${normalizedThreadKey}`,
      { markdown: "Agent stopped after making no visible progress. Please retry." },
    );
    expect(slack.postMessage).not.toHaveBeenCalledWith(
      `slack:${normalizedThreadKey}`,
      { markdown: "execution made no progress before silence deadline" },
    );
    expect(client.markFinalDelivered).toHaveBeenCalledWith("exe-silent", expect.any(String));
  });
});
