/**
 * Chat SDK bot definition — Slack adapter with Redis state.
 *
 * Handles @mentions and thread follow-ups. Streams Claude responses
 * back to Slack using native Slack streaming.
 */

import { Chat } from "chat";
import { createSlackAdapter } from "@chat-adapter/slack";
import { createRedisState } from "@chat-adapter/state-redis";
import { createMemoryState } from "@chat-adapter/state-memory";
import { runAgent } from "./agent";
import type { ModelMessage } from "ai";

function createBot() {
  const hasSlackCreds = process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET;

  const bot = new Chat({
    userName: "tempo-ai",
    adapters: hasSlackCreds
      ? { slack: createSlackAdapter() }
      : {},
    state: process.env.REDIS_URL
      ? createRedisState()
      : createMemoryState(),
  });

  /**
   * Build conversation history from thread messages for multi-turn context.
   */
  async function buildHistory(
    thread: Parameters<Parameters<typeof bot.onNewMention>[0]>[0],
    limit = 20
  ): Promise<ModelMessage[]> {
    try {
      const result = await thread.adapter.fetchMessages(thread.id, { limit });
      return [...result.messages]
        .reverse()
        .filter((msg) => msg.text.trim())
        .map((msg) => ({
          role: msg.author.isMe ? ("assistant" as const) : ("user" as const),
          content: msg.text,
        }));
    } catch {
      return [];
    }
  }

  // First @mention — subscribe to thread and respond
  bot.onNewMention(async (thread, message) => {
    await thread.subscribe();
    await thread.startTyping("Thinking...");

    const messages: ModelMessage[] = [{ role: "user", content: message.text }];
    const result = await runAgent(messages);
    await thread.post(result.textStream);
  });

  // Follow-up messages in subscribed threads
  bot.onSubscribedMessage(async (thread, message) => {
    if (!message.isMention) return;

    await thread.startTyping("Thinking...");

    const history = await buildHistory(thread);
    const messages: ModelMessage[] =
      history.length > 0 ? history : [{ role: "user", content: message.text }];

    const result = await runAgent(messages);
    await thread.post(result.textStream);
  });

  return bot;
}

// Lazy singleton — only create when first accessed at runtime
let _bot: ReturnType<typeof createBot> | null = null;
export function getBot() {
  if (!_bot) _bot = createBot();
  return _bot;
}
