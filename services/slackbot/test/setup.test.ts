import { afterEach, describe, expect, it, vi } from "vitest";

import { getSlackBootstrapState, registerPolicyTouchpointTrigger } from "../src/lib/bot/setup";

function createThread(id: string) {
  return {
    id,
    async subscribe() {},
    async startTyping() {},
    async post() {
      return {
        id: "msg-1",
        async edit() {},
      };
    },
  };
}

describe("registerPolicyTouchpointTrigger", () => {
  it("routes #touchpoint posts in #gigabrain-feed through the new-turn handler", async () => {
    let pattern: RegExp | undefined;
    let handler: ((thread: ReturnType<typeof createThread>, message: { text: string }) => Promise<void>) | undefined;
    const chat = {
      onNewMessage: vi.fn((registeredPattern: RegExp, registeredHandler: typeof handler) => {
        pattern = registeredPattern;
        handler = registeredHandler;
      }),
    };
    const bot = {
      onNewMention: vi.fn(async () => {}),
    };

    registerPolicyTouchpointTrigger(chat as any, bot as any);

    expect(pattern).toBeInstanceOf(RegExp);
    expect(pattern?.test("#touchpoint met with Sen. Example today")).toBe(true);

    await handler?.(createThread("C0AM0TR8N91:1700000000.000100"), { text: "#touchpoint met with Sen. Example today" });

    expect(bot.onNewMention).toHaveBeenCalledTimes(1);
  });

  it("ignores #touchpoint posts outside #gigabrain-feed", async () => {
    let handler: ((thread: ReturnType<typeof createThread>, message: { text: string }) => Promise<void>) | undefined;
    const chat = {
      onNewMessage: vi.fn((_pattern: RegExp, registeredHandler: typeof handler) => {
        handler = registeredHandler;
      }),
    };
    const bot = {
      onNewMention: vi.fn(async () => {}),
    };

    registerPolicyTouchpointTrigger(chat as any, bot as any);

    await handler?.(createThread("COTHER:1700000000.000100"), { text: "#touchpoint met with Sen. Example today" });

    expect(bot.onNewMention).not.toHaveBeenCalled();
  });
});

describe("getSlackBootstrapState", () => {
  const originalToken = process.env.SLACK_BOT_TOKEN;
  const originalSigningSecret = process.env.SLACK_SIGNING_SECRET;

  afterEach(() => {
    if (originalToken === undefined) delete process.env.SLACK_BOT_TOKEN;
    else process.env.SLACK_BOT_TOKEN = originalToken;
    if (originalSigningSecret === undefined) delete process.env.SLACK_SIGNING_SECRET;
    else process.env.SLACK_SIGNING_SECRET = originalSigningSecret;
  });

  it("treats local placeholder Slack tokens as not ready", () => {
    process.env.SLACK_BOT_TOKEN = "x" + "oxb-local-placeholder";
    process.env.SLACK_SIGNING_SECRET = "local-signing-secret";

    expect(getSlackBootstrapState()).toEqual({
      ready: false,
      missingEnvKeys: [],
      invalidEnvKeys: ["SLACK_BOT_TOKEN"],
    });
  });
});
