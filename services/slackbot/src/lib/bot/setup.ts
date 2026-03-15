import { Chat, type StreamChunk } from "chat";
import { createSlackAdapter, type SlackAdapter } from "@chat-adapter/slack";
import { createPostgresState } from "@chat-adapter/state-pg";
import { Pool } from "pg";
import { SlackBot, type SlackAdapter as BotSlackAdapter, type BotThread } from "./bot";

const hasSlackCreds = Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET);

let _instance: { chat: Chat; bot: SlackBot } | null = null;

function wrapAdapter(adapter: SlackAdapter): BotSlackAdapter {
  return {
    fetchMessage: (threadId, ts) => adapter.fetchMessage(threadId, ts) as any,
    setAssistantTitle: (channel, threadTs, title) => adapter.setAssistantTitle(channel, threadTs, title),
  };
}

function wrapThread(thread: any): BotThread {
  return {
    get id() { return thread.id; },
    subscribe: () => thread.subscribe(),
    startTyping: (status?: string) => thread.startTyping(status),
    post: async (content: AsyncGenerator<StreamChunk> | { markdown: string }, options?: { taskDisplayMode?: "timeline" | "plan" }) => {
      if (options?.taskDisplayMode && isAsyncIterable(content) && thread.adapter?.stream) {
        // Extract recipient fields from the thread's current message context
        // (same logic as chat's ThreadImpl.handleStream)
        const currentMsg = thread._currentMessage;
        const recipientUserId = currentMsg?.author?.userId;
        const recipientTeamId = currentMsg?.raw?.team_id ?? currentMsg?.raw?.team;

        const raw = await thread.adapter.stream(
          thread.id,
          content as AsyncIterable<string | StreamChunk>,
          { taskDisplayMode: options.taskDisplayMode, recipientUserId, recipientTeamId },
        );
        return {
          id: raw.id ?? "unknown",
          edit: async (c: { markdown: string }) => { await thread.adapter.editMessage(thread.id, raw.id, c); },
        };
      }
      return thread.post(content);
    },
  };
}

function isAsyncIterable(v: unknown): v is AsyncIterable<unknown> {
  return v != null && typeof v === "object" && Symbol.asyncIterator in v;
}

function create() {
  const pool = new Pool({ connectionString: process.env.DATABASE_URL, max: 10 });

  const chat = new Chat({
    userName: process.env.SLACK_BOT_USERNAME || "ai-agent",
    adapters: hasSlackCreds ? { slack: createSlackAdapter() } : {},
    state: createPostgresState({ client: pool }),
    onLockConflict: "force",
  } as ConstructorParameters<typeof Chat>[0]);

  const slack = hasSlackCreds ? chat.getAdapter("slack") as SlackAdapter : undefined;
  const bot = SlackBot.createFromEnv(slack ? wrapAdapter(slack) : undefined);

  chat.onNewMention((t, m) => bot.onNewMention(wrapThread(t), m as any));
  chat.onSubscribedMessage((t, m) => bot.onSubscribedMessage(wrapThread(t), m as any));

  return { chat, bot };
}

export function getBot() {
  if (!_instance) _instance = create();
  return _instance.chat;
}

export function getSlackBootstrapState() {
  const required = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"] as const;
  const missing = required.filter((k) => !process.env[k]?.trim());
  return { ready: missing.length === 0, missingEnvKeys: [...missing] };
}
