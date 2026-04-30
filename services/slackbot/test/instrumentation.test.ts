import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const mockSetup = vi.hoisted(() => ({
  ensureBotReady: vi.fn(),
  getSlackBootstrapState: vi.fn(),
}));
const mockLog = vi.hoisted(() => ({
  error: vi.fn(),
  warn: vi.fn(),
}));

vi.mock("@/lib/bot/setup", () => mockSetup);
vi.mock("@/lib/logger", () => ({ log: mockLog }));

const originalRuntime = process.env.NEXT_RUNTIME;
const originalToken = process.env.SLACK_BOT_TOKEN;
const originalSigningSecret = process.env.SLACK_SIGNING_SECRET;

beforeEach(() => {
  vi.clearAllMocks();
  vi.resetModules();
});

afterEach(() => {
  if (originalRuntime === undefined) {
    delete process.env.NEXT_RUNTIME;
  } else {
    process.env.NEXT_RUNTIME = originalRuntime;
  }
  if (originalToken === undefined) delete process.env.SLACK_BOT_TOKEN;
  else process.env.SLACK_BOT_TOKEN = originalToken;
  if (originalSigningSecret === undefined) delete process.env.SLACK_SIGNING_SECRET;
  else process.env.SLACK_SIGNING_SECRET = originalSigningSecret;
  vi.resetModules();
});

function flushPromises(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

describe("instrumentation", () => {
  it("logs async startup init failures without failing the health server", async () => {
    process.env.NEXT_RUNTIME = "nodejs";
    mockSetup.ensureBotReady.mockImplementation(async () => {
      throw new Error("invalid_auth");
    });
    mockSetup.getSlackBootstrapState.mockReturnValue({ ready: true, missingEnvKeys: [], invalidEnvKeys: [] });

    const { register } = await import("../src/instrumentation");

    await expect(register()).resolves.toBeUndefined();
    await flushPromises();
    expect(mockSetup.ensureBotReady).toHaveBeenCalledOnce();
    expect(mockLog.error).toHaveBeenCalledWith("slackbot_startup_initialize_failed", {
      error: "invalid_auth",
    });
  });

  it("skips startup init when Slack bootstrap is not ready", async () => {
    process.env.NEXT_RUNTIME = "nodejs";
    process.env.SLACK_BOT_TOKEN = "local-placeholder-token";
    process.env.SLACK_SIGNING_SECRET = "local-signing-secret";
    mockSetup.ensureBotReady.mockResolvedValue(undefined);
    mockSetup.getSlackBootstrapState.mockReturnValue({
      ready: false,
      missingEnvKeys: [],
      invalidEnvKeys: ["SLACK_BOT_TOKEN"],
    });

    const { register } = await import("../src/instrumentation");

    await register();
    expect(mockSetup.ensureBotReady).not.toHaveBeenCalled();
    expect(mockLog.warn).toHaveBeenCalledWith("slackbot_startup_initialize_skipped", {
      missing_env_keys: [],
      invalid_env_keys: ["SLACK_BOT_TOKEN"],
    });
  });
});
