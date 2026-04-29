import { afterEach, describe, expect, it, vi } from "vitest";

const originalRuntime = process.env.NEXT_RUNTIME;

afterEach(() => {
  if (originalRuntime === undefined) {
    delete process.env.NEXT_RUNTIME;
  } else {
    process.env.NEXT_RUNTIME = originalRuntime;
  }
  vi.resetModules();
  vi.doUnmock("@/lib/bot/setup");
  vi.doUnmock("@/lib/logger");
});

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

describe("instrumentation", () => {
  it("logs async startup init failures without failing the health server", async () => {
    process.env.NEXT_RUNTIME = "nodejs";
    const ensureBotReady = vi.fn(async () => {
      throw new Error("invalid_auth");
    });
    const logError = vi.fn();

    vi.doMock("@/lib/bot/setup", () => ({
      ensureBotReady,
      getSlackBootstrapState: () => ({ ready: true, missingEnvKeys: [], invalidEnvKeys: [] }),
    }));
    vi.doMock("@/lib/logger", () => ({ log: { error: logError } }));

    const { register } = await import("../src/instrumentation");

    await expect(register()).resolves.toBeUndefined();
    await flushPromises();
    expect(ensureBotReady).toHaveBeenCalledOnce();
    expect(logError).toHaveBeenCalledWith("slackbot_startup_initialize_failed", {
      error: "invalid_auth",
    });
  });

  it("skips startup init when Slack bootstrap is not ready", async () => {
    process.env.NEXT_RUNTIME = "nodejs";
    const ensureBotReady = vi.fn();
    const logWarn = vi.fn();

    vi.doMock("@/lib/bot/setup", () => ({
      ensureBotReady,
      getSlackBootstrapState: () => ({
        ready: false,
        missingEnvKeys: [],
        invalidEnvKeys: ["SLACK_BOT_TOKEN"],
      }),
    }));
    vi.doMock("@/lib/logger", () => ({ log: { error: vi.fn(), warn: logWarn } }));

    const { register } = await import("../src/instrumentation");

    await register();
    expect(ensureBotReady).not.toHaveBeenCalled();
    expect(logWarn).toHaveBeenCalledWith("slackbot_startup_initialize_skipped", {
      missing_env_keys: [],
      invalid_env_keys: ["SLACK_BOT_TOKEN"],
    });
  });
});
