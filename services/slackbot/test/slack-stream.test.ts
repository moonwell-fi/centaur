import { beforeEach, describe, expect, it, vi } from "vitest";

import { BoltSlackApp } from "../src/lib/slack/app";
import { classifySlackError, SlackApiCallError } from "../src/lib/slack/errors";
import type { StreamChunk } from "../src/lib/slack/types";

const slackApiCall = vi.hoisted(() => vi.fn());
const slackUsersInfo = vi.hoisted(() => vi.fn());

vi.mock("@slack/web-api", () => ({
  WebClient: class WebClient {
    apiCall = slackApiCall;

    auth = {
      test: vi.fn(async () => ({ ok: true, user_id: "UBOT" })),
    };

    users = {
      info: slackUsersInfo,
    };
  },
}));

vi.mock("@slack/bolt", () => ({
  App: class App {
    event = vi.fn();

    processEvent = vi.fn();
  },
  verifySlackRequest: vi.fn(),
}));

function createAdapter() {
  return new BoltSlackApp("xoxb-test", "signing-secret").getSlackAdapter() as unknown as {
    stream(
      threadId: string,
      stream: AsyncIterable<string | StreamChunk>,
      options?: { taskDisplayMode?: "timeline" | "plan" },
    ): Promise<{ id: string }>;
  };
}

function streamCallParams(method: string): Record<string, unknown>[] {
  return slackApiCall.mock.calls
    .filter(([calledMethod]) => calledMethod === method)
    .map(([, params]) => params as Record<string, unknown>);
}

describe("Slack stream payloads", () => {
  beforeEach(() => {
    slackApiCall.mockReset();
    slackUsersInfo.mockReset();
    slackApiCall.mockImplementation(async (method: string) => ({
      ok: true,
      ...(method === "chat.startStream" ? { ts: "1700000000.000100" } : {}),
    }));
    slackUsersInfo.mockResolvedValue({ ok: true, user: { name: "alice" } });
  });

  it("uses chunk-mode for markdown and structured updates", async () => {
    const adapter = createAdapter();

    await adapter.stream("slack:C123:1700000000.000001", (async function* () {
      yield { type: "markdown_text", text: "\u200b" } satisfies StreamChunk;
      yield { type: "plan_update", title: "Completed" } satisfies StreamChunk;
      yield { type: "markdown_text", text: "pong" } satisfies StreamChunk;
    })(), { taskDisplayMode: "plan" });

    const start = streamCallParams("chat.startStream")[0];
    const appends = streamCallParams("chat.appendStream");

    expect(start).toEqual(expect.objectContaining({
      chunks: [{ type: "markdown_text", text: "\u200b" }],
    }));
    expect(start).not.toHaveProperty("markdown_text");
    expect(appends[0]).toEqual(expect.objectContaining({
      chunks: [{ type: "plan_update", title: "Completed" }],
    }));
    expect(appends[0]).not.toHaveProperty("markdown_text");
    expect(appends[1]).toEqual(expect.objectContaining({
      chunks: [{ type: "markdown_text", text: "pong" }],
    }));
    expect(appends[1]).not.toHaveProperty("markdown_text");
  });

  it("can start directly with a structured chunk", async () => {
    const adapter = createAdapter();

    await adapter.stream("slack:C123:1700000000.000001", (async function* () {
      yield { type: "plan_update", title: "Working" } satisfies StreamChunk;
    })());

    const start = streamCallParams("chat.startStream")[0];
    const appends = streamCallParams("chat.appendStream");

    expect(start).toEqual(expect.objectContaining({
      chunks: [{ type: "plan_update", title: "Working" }],
    }));
    expect(start).not.toHaveProperty("markdown_text");
    expect(appends).toHaveLength(0);
  });

  it("falls back to raw mention IDs when users.info cannot resolve a user", async () => {
    slackUsersInfo.mockResolvedValueOnce({ ok: false, error: "user_not_found" });
    const adapter = new BoltSlackApp("xoxb-test", "signing-secret").getSlackAdapter() as any;

    const message = await adapter.toBotMessage(
      "slack:C123:1700000000.000001",
      {
        type: "app_mention",
        text: "hi <@U404>",
        user: "U123",
        ts: "1700000000.000001",
      },
    );

    expect(message.text).toContain("U404");
  });
});

describe("Slack error classification", () => {
  it.each([
    ["channel_not_found", "invalid_destination", false],
    ["not_in_channel", "invalid_destination", false],
    ["user_not_found", "invalid_destination", false],
    ["restricted_action", "restricted_destination", false],
    ["restricted_action_thread_locked", "restricted_destination", false],
    ["invalid_blocks", "invalid_payload", false],
    ["msg_too_long", "invalid_payload", false],
    ["rate_limited", "rate_limited", true],
    ["internal_error", "transient_slack_error", true],
  ])("classifies Slack code %s", (code, errorClass, retryable) => {
    const result = classifySlackError(new SlackApiCallError("chat.postMessage", code, {
      ok: false,
      error: code,
    }));

    expect(result).toMatchObject({
      code,
      errorClass,
      retryable,
    });
  });

  it("treats 409 idempotency conflicts as non-retryable duplicates", () => {
    const result = classifySlackError({
      message: "Request failed with status code 409",
      response: { status: 409 },
    });

    expect(result.errorClass).toBe("duplicate_or_conflict");
    expect(result.retryable).toBe(false);
  });

  it("falls back to message matching for observed Slack error strings", () => {
    const result = classifySlackError(new Error("An API error occurred: user_not_found"));

    expect(result.errorClass).toBe("invalid_destination");
    expect(result.retryable).toBe(false);
  });
});
