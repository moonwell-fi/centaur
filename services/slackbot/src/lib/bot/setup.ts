import { log } from "@/lib/logger";
import {
  BoltSlackApp,
  policyTouchpointChannelId,
  policyTouchpointPattern,
} from "@/lib/slack/app";
import { SlackBot } from "./bot";

let _instance: { app: BoltSlackApp; ready: Promise<void> } | null = null;

export function registerPolicyTouchpointTrigger(
  chat: Pick<{
    onNewMessage(
      pattern: RegExp,
      handler: (thread: { id: string }, message: { text: string }) => Promise<void>,
    ): void;
  }, "onNewMessage">,
  bot: Pick<SlackBot, "onNewMention">,
) {
  chat.onNewMessage(policyTouchpointPattern, async (thread, message) => {
    const id = thread.id.startsWith("slack:") ? thread.id.slice("slack:".length) : thread.id;
    if (!id.startsWith(`${policyTouchpointChannelId}:`)) return;
    await bot.onNewMention(thread as any, message as any);
  });
}

function create() {
  const app = new BoltSlackApp(
    process.env.SLACK_BOT_TOKEN || "",
    process.env.SLACK_SIGNING_SECRET || "",
  );
  const ready = app.init();
  void ready.catch((error) => {
    log.error("slackbot_initialize_failed", {
      error: error instanceof Error ? error.message : String(error),
    });
  });

  return { app, ready };
}

export function getBot() {
  if (!_instance) _instance = create();
  return _instance.app;
}

export async function ensureBotReady() {
  if (!_instance) _instance = create();
  await _instance.ready;
  return _instance.app;
}

export function getSlackBootstrapState() {
  const required = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"] as const;
  const missing = required.filter((k) => !process.env[k]?.trim());
  const slackBotToken = process.env.SLACK_BOT_TOKEN?.trim() || "";
  const hasUsableSlackBotToken = slackBotToken.startsWith("xoxb-") && slackBotToken.length >= 40;
  const invalid = missing.includes("SLACK_BOT_TOKEN") || hasUsableSlackBotToken
    ? []
    : ["SLACK_BOT_TOKEN"];
  return {
    ready: missing.length === 0 && invalid.length === 0,
    missingEnvKeys: [...missing],
    invalidEnvKeys: invalid,
  };
}
