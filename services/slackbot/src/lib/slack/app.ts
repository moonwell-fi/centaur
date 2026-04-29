import { App, verifySlackRequest, type Receiver, type ReceiverEvent } from "@slack/bolt";
import { normalizeThreadKey } from "@centaur/harness-events";
import { WebClient } from "@slack/web-api";
import { NextRequest, NextResponse } from "next/server";

import { log } from "@/lib/logger";
import { SlackBot, type BotAttachment, type BotMessage, type BotThread, type SlackAdapter } from "@/lib/bot/bot";
import {
  markdownToPlainText,
  renderMarkdownForSlack,
  slackMrkdwnToAst,
  type Root,
} from "./markdown";
import type { StreamChunk } from "./types";

const PENDING_SUBSCRIPTION_TTL_MS = 2 * 60_000;
const SEEN_EVENT_TTL_MS = 10 * 60_000;
const STREAM_BOOTSTRAP_TEXT = "\u200b";
const POLICY_TOUCHPOINT_CHANNEL_ID = "C0AM0TR8N91";
const POLICY_TOUCHPOINT_PATTERN = /(^|\s)#touchpoint\b/i;

type SlackMessageEvent = {
  type: string;
  subtype?: string;
  text?: string;
  user?: string;
  bot_id?: string;
  channel?: string;
  channel_type?: string;
  ts?: string;
  thread_ts?: string;
  team?: string;
  team_id?: string;
  files?: SlackFile[];
};

type SlackFile = {
  mimetype?: string;
  url_private?: string;
  name?: string;
  size?: number;
  original_w?: number;
  original_h?: number;
};

type SlackEventEnvelope = {
  type?: string;
  challenge?: string;
  team_id?: string;
  event_id?: string;
  event?: Record<string, unknown>;
  ssl_check?: string | boolean;
};

type WaitUntilOptions = {
  waitUntil?: (task: Promise<unknown>) => void;
};

type ThreadContext = {
  recipientUserId?: string;
  recipientTeamId?: string;
};

type SlackHistoryMessage = {
  id?: string;
  text: string;
  formatted?: Root;
  raw?: { ts?: string; team_id?: string; team?: string };
  author: { isMe: boolean; isBot: boolean; userId: string };
  attachments?: BotAttachment[];
};

function threadIdFromEvent(event: SlackMessageEvent): string | null {
  if (!event.channel) return null;
  const threadTs = event.thread_ts || event.ts;
  if (!threadTs) return null;
  return `slack:${event.channel}:${threadTs}`;
}

function splitSlackThreadId(threadId: string): { channel: string; threadTs: string } {
  const parts = threadId.split(":");
  if (parts.length !== 3 || parts[0] !== "slack" || !parts[1] || !parts[2]) {
    throw new Error(`Invalid Slack thread id: ${threadId}`);
  }
  return { channel: parts[1], threadTs: parts[2] };
}

function isIgnoredMessageSubtype(subtype?: string): boolean {
  return new Set([
    "message_changed",
    "message_deleted",
    "message_replied",
    "channel_join",
    "channel_leave",
    "channel_topic",
    "channel_purpose",
    "channel_name",
    "channel_archive",
    "channel_unarchive",
    "group_join",
    "group_leave",
    "group_topic",
    "group_purpose",
    "group_name",
    "group_archive",
    "group_unarchive",
    "ekm_access_denied",
    "tombstone",
  ]).has(subtype || "");
}

class NextSlackReceiver implements Receiver {
  private app: App | null = null;

  init(app: App): void {
    this.app = app;
  }

  async start(): Promise<void> {}

  async stop(): Promise<void> {}

  async dispatch(event: ReceiverEvent): Promise<void> {
    if (!this.app) throw new Error("Slack receiver not initialized");
    await this.app.processEvent(event);
  }
}

class WebClientSlackAdapter implements SlackAdapter {
  private readonly userCache = new Map<string, { displayName: string; realName: string }>();

  private botUserId = "";

  constructor(
    private readonly client: WebClient,
    private readonly botToken: string,
  ) {}

  async init(): Promise<void> {
    const auth = await this.client.auth.test();
    this.botUserId = typeof auth.user_id === "string" ? auth.user_id : "";
  }

  getBotUserId(): string {
    return this.botUserId;
  }

  async fetchMessage(threadId: string, ts: string): Promise<{ attachments?: BotAttachment[] } | null> {
    const { channel, threadTs } = splitSlackThreadId(threadId);
    const response = await this.call<{ messages?: SlackMessageEvent[] }>("conversations.replies", {
      channel,
      ts: threadTs,
      oldest: ts,
      inclusive: true,
      limit: 1,
    });
    const target = response.messages?.find((message) => message.ts === ts);
    if (!target) return null;
    return { attachments: (target.files || []).map((file) => this.createAttachment(file)) };
  }

  async fetchMessages(
    threadId: string,
    options?: { direction?: "forward" | "backward"; limit?: number },
  ): Promise<{ messages: SlackHistoryMessage[] }> {
    const { channel, threadTs } = splitSlackThreadId(threadId);
    const limit = options?.limit || 100;
    const response = await this.call<{ messages?: SlackMessageEvent[] }>("conversations.replies", {
      channel,
      ts: threadTs,
      limit,
    });

    const messages = await Promise.all(
      (response.messages || []).map((message) => this.toBotMessage(threadId, message, { skipSelfMention: false })),
    ) as SlackHistoryMessage[];

    return {
      messages: options?.direction === "backward" ? messages.slice(-limit) : messages,
    };
  }

  async postMessage(threadId: string, message: { markdown: string }): Promise<{ id: string }> {
    const { channel, threadTs } = splitSlackThreadId(threadId);
    const rendered = renderMarkdownForSlack(message.markdown);
    const response = await this.call<{ ts?: string }>("chat.postMessage", {
      channel,
      thread_ts: threadTs,
      text: rendered.text || STREAM_BOOTSTRAP_TEXT,
      ...(rendered.blocks ? { blocks: rendered.blocks } : {}),
      unfurl_links: false,
      unfurl_media: false,
    });
    return { id: String(response.ts || "") };
  }

  async updateMessage(threadId: string, messageId: string, message: { markdown: string }): Promise<void> {
    const { channel } = splitSlackThreadId(threadId);
    const rendered = renderMarkdownForSlack(message.markdown);
    await this.call("chat.update", {
      channel,
      ts: messageId,
      text: rendered.text || STREAM_BOOTSTRAP_TEXT,
      ...(rendered.blocks ? { blocks: rendered.blocks } : {}),
      unfurl_links: false,
      unfurl_media: false,
    });
  }

  async stream(
    threadId: string,
    stream: AsyncIterable<string | StreamChunk>,
    options?: { recipientUserId?: string; recipientTeamId?: string; taskDisplayMode?: "timeline" | "plan" },
  ): Promise<{ id: string }> {
    const iterator = stream[Symbol.asyncIterator]();
    const first = await iterator.next();
    if (first.done) {
      return this.postMessage(threadId, { markdown: "" });
    }

    const { channel, threadTs } = splitSlackThreadId(threadId);
    const start = await this.call<{ ts?: string }>("chat.startStream", {
      channel,
      thread_ts: threadTs,
      ...(channel.startsWith("D")
        ? {}
        : {
            recipient_user_id: options?.recipientUserId,
            recipient_team_id: options?.recipientTeamId,
          }),
      ...(options?.taskDisplayMode ? { task_display_mode: options.taskDisplayMode } : {}),
      ...this.streamPayloadForChunk(first.value),
    });
    const ts = String(start.ts || "");

    while (true) {
      const next = await iterator.next();
      if (next.done) break;
      await this.call("chat.appendStream", {
        channel,
        ts,
        ...this.streamPayloadForChunk(next.value),
      });
    }

    await this.call("chat.stopStream", { channel, ts });
    return { id: ts };
  }

  async setAssistantTitle(channel: string, threadTs: string, title: string): Promise<void> {
    await this.call("assistant.threads.setTitle", {
      channel_id: channel,
      thread_ts: threadTs,
      title,
    });
  }

  async startTyping(threadId: string, status?: string): Promise<void> {
    const { channel, threadTs } = splitSlackThreadId(threadId);
    await this.call("assistant.threads.setStatus", {
      channel_id: channel,
      thread_ts: threadTs,
      status: status || "Typing...",
      loading_messages: [status || "Typing..."],
    });
  }

  async getInstallation(): Promise<{ botToken: string } | null> {
    return { botToken: this.botToken };
  }

  async withBotToken<T>(_token: string, fn: () => Promise<T> | T): Promise<T> {
    return await fn();
  }

  async toBotMessage(
    _threadId: string,
    event: SlackMessageEvent,
    options?: { skipSelfMention?: boolean },
  ): Promise<BotMessage> {
    const mrkdwn = await this.resolveInlineMentions(event.text || "", options?.skipSelfMention ?? true);
    return {
      id: event.ts,
      text: markdownToPlainText(mrkdwn),
      formatted: slackMrkdwnToAst(mrkdwn),
      raw: {
        ts: event.ts,
        team_id: event.team_id ?? event.team,
        team: event.team,
      },
      author: {
        isMe: event.user === this.botUserId || event.bot_id === this.botUserId,
        isBot: Boolean(event.bot_id),
        userId: event.user || "",
      },
      attachments: (event.files || []).map((file) => this.createAttachment(file)),
    };
  }

  private async resolveInlineMentions(text: string, skipSelfMention: boolean): Promise<string> {
    const ids = [...new Set(Array.from(text.matchAll(/<@([A-Z0-9_]+)>/g)).map((match) => match[1]))];
    if (ids.length === 0) return text;

    const replacements = new Map<string, string>();
    for (const id of ids) {
      if (skipSelfMention && id === this.botUserId) continue;
      const user = await this.lookupUser(id);
      if (user.displayName) replacements.set(id, `<@${id}|${user.displayName}>`);
    }

    if (replacements.size === 0) return text;
    return text.replace(/<@([A-Z0-9_]+)>/g, (full, id: string) => replacements.get(id) || full);
  }

  private async lookupUser(userId: string): Promise<{ displayName: string; realName: string }> {
    const cached = this.userCache.get(userId);
    if (cached) return cached;

    const result = await this.client.users.info({ user: userId });
    const user = result.user as { name?: string; profile?: { display_name?: string; real_name?: string } } | undefined;
    const resolved = {
      displayName: user?.profile?.display_name || user?.profile?.real_name || user?.name || userId,
      realName: user?.profile?.real_name || user?.name || userId,
    };
    this.userCache.set(userId, resolved);
    return resolved;
  }

  private createAttachment(file: SlackFile): BotAttachment {
    const url = file.url_private;
    return {
      url,
      name: file.name,
      mimeType: file.mimetype,
      fetchData: url
        ? async () => {
            const response = await fetch(url, {
              headers: { Authorization: `Bearer ${this.botToken}` },
            });
            if (!response.ok) {
              throw new Error(`Failed to fetch Slack file: ${response.status} ${response.statusText}`);
            }
            return Buffer.from(await response.arrayBuffer());
          }
        : undefined,
    };
  }

  private streamPayloadForChunk(chunk: string | StreamChunk): { markdown_text: string; chunks?: StreamChunk[] } {
    if (typeof chunk === "string") return { markdown_text: chunk || STREAM_BOOTSTRAP_TEXT };
    if (chunk.type === "markdown_text") return { markdown_text: chunk.text || STREAM_BOOTSTRAP_TEXT };
    return { markdown_text: STREAM_BOOTSTRAP_TEXT, chunks: [chunk] };
  }

  private async call<T = Record<string, unknown>>(method: string, params: Record<string, unknown>): Promise<T> {
    const result = await this.client.apiCall(method, params);
    if (!result || result.ok !== true) {
      const error = typeof result?.error === "string" ? result.error : `Slack ${method} failed`;
      throw new Error(error);
    }
    return result as T;
  }
}

export class BoltSlackApp {
  private readonly receiver = new NextSlackReceiver();

  private readonly adapter: WebClientSlackAdapter;

  private readonly bolt: App;

  private readonly bot: SlackBot;

  private readonly queue = new Map<string, Promise<void>>();

  private readonly pendingSubscriptions = new Map<string, number>();

  private readonly seenEvents = new Map<string, number>();

  private ready: Promise<void> | null = null;

  constructor(
    private readonly botToken: string,
    private readonly signingSecret: string,
  ) {
    const client = new WebClient(botToken);
    this.adapter = new WebClientSlackAdapter(client, botToken);
    this.bot = SlackBot.createFromEnv(this.adapter);
    this.bolt = new App({
      token: botToken,
      receiver: this.receiver,
      ignoreSelf: false,
    });
    this.registerListeners();
  }

  async init(): Promise<void> {
    if (!this.ready) {
      this.ready = this.adapter.init()
        .then(() => {
          this.bot.startFinalDeliveryWorker();
        })
        .catch((error) => {
          this.ready = null;
          throw error;
        });
    }
    await this.ready;
  }

  async handleRequest(request: NextRequest, options?: WaitUntilOptions): Promise<NextResponse> {
    const body = await request.text();
    try {
      verifySlackRequest({
        signingSecret: this.signingSecret,
        body,
        headers: {
          "x-slack-signature": request.headers.get("x-slack-signature") || "",
          "x-slack-request-timestamp": Number(request.headers.get("x-slack-request-timestamp")),
        },
      });
    } catch {
      return NextResponse.json({ error: "invalid signature" }, { status: 401 });
    }

    const payload = JSON.parse(body) as SlackEventEnvelope;
    if (payload.type === "url_verification") {
      return NextResponse.json({ challenge: payload.challenge });
    }
    if (payload.ssl_check) {
      return new NextResponse(null, { status: 200 });
    }
    if (payload.type !== "event_callback" || !payload.event) {
      return NextResponse.json({ ok: true });
    }
    if (payload.event_id && this.seenRecently(payload.event_id)) {
      return NextResponse.json({ ok: true, duplicate: true });
    }

    const task = this.receiver.dispatch({
      body: payload as Record<string, unknown>,
      ack: async () => {},
      retryNum: numberHeader(request.headers.get("x-slack-retry-num")),
      retryReason: request.headers.get("x-slack-retry-reason") || undefined,
    }).catch((error) => {
      log.error("slack_bolt_dispatch_failed", {
        event_id: payload.event_id,
        event_type: (payload.event as SlackMessageEvent).type,
        error: error instanceof Error ? error.message : String(error),
      });
    });

    if (options?.waitUntil) options.waitUntil(task);
    else void task;

    return NextResponse.json({ ok: true });
  }

  getSlackAdapter(): SlackAdapter {
    return this.adapter;
  }

  private registerListeners(): void {
    this.bolt.event("assistant_thread_started", async ({ event }: any) => {
      log.info("slack_assistant_event_received", { event_type: event.type });
    });
    this.bolt.event("assistant_thread_context_changed", async ({ event }: any) => {
      log.info("slack_assistant_event_received", { event_type: event.type });
    });
    this.bolt.event("app_mention", async ({ event, body }: any) => {
      await this.routeSlackEvent(event as SlackMessageEvent, body?.team_id);
    });
    this.bolt.event("message", async ({ event, body }: any) => {
      await this.routeSlackEvent(event as SlackMessageEvent, body?.team_id);
    });
  }

  private async routeSlackEvent(event: SlackMessageEvent, teamId?: string): Promise<void> {
    const threadId = threadIdFromEvent(event);
    if (!threadId) return;
    await this.enqueue(threadId, () => this.processMessageEvent(event, threadId, teamId));
  }

  private async processMessageEvent(
    event: SlackMessageEvent,
    threadId: string,
    teamId?: string,
  ): Promise<void> {
    if (event.type !== "message" && event.type !== "app_mention") return;
    if (isIgnoredMessageSubtype(event.subtype)) return;

    const isSubscribed = await this.isSubscribedThread(threadId);
    const isPolicyTouchpoint = Boolean(event.channel === POLICY_TOUCHPOINT_CHANNEL_ID && POLICY_TOUCHPOINT_PATTERN.test(event.text || ""));
    const isDirectMessage = event.channel_type === "im";
    const isMention = event.type === "app_mention"
      || isPolicyTouchpoint
      || isDirectMessage
      || this.messageMentionsBot(event.text || "");

    if (!isSubscribed && !isMention) return;

    const thread = this.createThread(threadId, {
      recipientUserId: event.user,
      recipientTeamId: event.team_id ?? event.team ?? teamId,
    });
    const message = await this.adapter.toBotMessage(threadId, {
      ...event,
      team_id: event.team_id ?? teamId,
    }, { skipSelfMention: event.type !== "app_mention" });
    message.isMention = isMention;

    if (isSubscribed) {
      await this.bot.onSubscribedMessage(thread, message);
      return;
    }

    await this.bot.onNewMention(thread, message);
  }

  private createThread(threadId: string, context: ThreadContext): BotThread {
    return {
      id: threadId,
      subscribe: async () => {
        this.pendingSubscriptions.set(threadId, Date.now() + PENDING_SUBSCRIPTION_TTL_MS);
      },
      startTyping: async (status?: string) => {
        try {
          await this.adapter.startTyping(threadId, status);
        } catch (error) {
          log.warn("slack_start_typing_failed", {
            thread_id: threadId,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      },
      post: async (
        content: AsyncGenerator<StreamChunk> | { markdown: string },
        options?: { taskDisplayMode?: "timeline" | "plan" },
      ) => {
        if ("markdown" in content) {
          const posted = await this.adapter.postMessage(threadId, content);
          return {
            id: posted.id,
            edit: async (update: { markdown: string }) => {
              await this.adapter.updateMessage(threadId, posted.id, update);
            },
          };
        }

        const streamed = await this.adapter.stream(threadId, content, {
          recipientUserId: context.recipientUserId,
          recipientTeamId: context.recipientTeamId,
          taskDisplayMode: options?.taskDisplayMode,
        });
        return {
          id: streamed.id,
          edit: async (update: { markdown: string }) => {
            await this.adapter.updateMessage(threadId, streamed.id, update);
          },
        };
      },
    };
  }

  private messageMentionsBot(text: string): boolean {
    const botUserId = this.adapter.getBotUserId();
    return Boolean(botUserId && text.includes(`<@${botUserId}>`));
  }

  private async isSubscribedThread(threadId: string): Promise<boolean> {
    const pendingUntil = this.pendingSubscriptions.get(threadId);
    if (pendingUntil && pendingUntil > Date.now()) return true;
    if (pendingUntil) this.pendingSubscriptions.delete(threadId);

    try {
      const result = await this.bot.client.getMessages(normalizeThreadKey(threadId), { limit: 1 });
      const subscribed = result.messages.length > 0;
      if (subscribed) this.pendingSubscriptions.delete(threadId);
      return subscribed;
    } catch (error) {
      log.warn("slack_subscription_lookup_failed", {
        thread_id: threadId,
        error: error instanceof Error ? error.message : String(error),
      });
      return false;
    }
  }

  private enqueue(threadId: string, taskFactory: () => Promise<void>): Promise<void> {
    const previous = this.queue.get(threadId) || Promise.resolve();
    const next = previous
      .catch(() => {})
      .then(taskFactory)
      .finally(() => {
        if (this.queue.get(threadId) === next) this.queue.delete(threadId);
      });
    this.queue.set(threadId, next);
    return next;
  }

  private seenRecently(eventId: string): boolean {
    const now = Date.now();
    for (const [seenId, expiresAt] of this.seenEvents) {
      if (expiresAt <= now) this.seenEvents.delete(seenId);
    }

    const expiresAt = this.seenEvents.get(eventId);
    if (expiresAt && expiresAt > now) return true;
    this.seenEvents.set(eventId, now + SEEN_EVENT_TTL_MS);
    return false;
  }
}

function numberHeader(value: string | null): number | undefined {
  if (!value) return undefined;
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : undefined;
}

export const policyTouchpointPattern = POLICY_TOUCHPOINT_PATTERN;
export const policyTouchpointChannelId = POLICY_TOUCHPOINT_CHANNEL_ID;
