import { Chat } from "chat";
import { createSlackAdapter, type SlackAdapter } from "@chat-adapter/slack";
import { createPostgresState } from "@chat-adapter/state-pg";
import { normalizeThreadKey, splitThreadKey } from "@centaur/harness-events";
import { CentaurClient } from "@centaur/api-client";
import type { InputContentBlock } from "@centaur/api-client";
import { AxiosError } from "axios";
import type { StreamChunk } from "chat";
import { Pool } from "pg";
import { log } from "@/lib/logger";
import { ProgressTracker } from "./progress-tracker";

type Thread = Parameters<Parameters<Chat["onNewMention"]>[0]>[0];
type Message = Parameters<Parameters<Chat["onNewMention"]>[0]>[1];
type Attachment = NonNullable<Message["attachments"]>[number];

export class SlackBot {
  readonly chat: Chat;
  readonly client: CentaurClient;
  private viewerUrl: string;

  constructor(opts: { client: CentaurClient; pool: Pool; userName?: string; viewerUrl?: string }) {
    this.client = opts.client;
    this.viewerUrl = opts.viewerUrl || "";
    this.chat = new Chat({
      userName: opts.userName || "ai-agent",
      adapters: Boolean(process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET)
        ? { slack: createSlackAdapter() } : {},
      state: createPostgresState({ client: opts.pool }),
      onLockConflict: "force",
    } as ConstructorParameters<typeof Chat>[0]);

    this.chat.onNewMention((t, m) => this.onNewMention(t, m));
    this.chat.onSubscribedMessage((t, m) => this.onSubscribedMessage(t, m));
  }

  static createFromEnv(): SlackBot {
    return new SlackBot({
      client: new CentaurClient({
        apiUrl: process.env.CENTAUR_API_URL || "http://api:8000",
        apiKey: process.env.SLACKBOT_API_KEY || "",
        logger: log,
      }),
      pool: new Pool({ connectionString: process.env.DATABASE_URL, max: 10 }),
      userName: process.env.SLACK_BOT_USERNAME || "ai-agent",
      viewerUrl: process.env.THREAD_VIEWER_URL || "",
    });
  }

  static getBootstrapState() {
    const required = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"] as const;
    const missing = required.filter((k) => !process.env[k]?.trim());
    return { ready: missing.length === 0, missingEnvKeys: [...missing] };
  }

  // ── Handlers ────────────────────────────────────────────────────────────

  async onNewMention(thread: Thread, msg: Message) {
    if (msg.author.isMe || msg.author.isBot) return;
    await thread.subscribe();
    const attachments = await this.loadAttachments(thread, msg);
    await this.executeTurn(thread, msg.text, attachments, msg.author.userId);
  }

  async onSubscribedMessage(thread: Thread, msg: Message) {
    if (msg.author.isMe || msg.author.isBot) return;

    if (msg.isMention) {
      await this.executeTurn(thread, msg.text, msg.attachments || [], msg.author.userId);
      return;
    }

    const text = (msg.text || "").trim();
    const files = (msg.attachments || [])
      .filter((a) => !!a.url && !!a.name)
      .map((a) => ({ url: a.url!, name: a.name!, mimeType: a.mimeType }));
    if (!text && files.length === 0) return;

    try {
      await this.client.postContext({
        threadKey: normalizeThreadKey(thread.id),
        text: text || "Shared attachment in thread.",
        userId: msg.author.userId,
        attachments: files.length > 0 ? files : undefined,
      });
    } catch (err) {
      log.warn("thread_context_post_failed", {
        thread: thread.id,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  // ── Core ─────────────────────────────────────────────────────────────────

  async executeTurn(thread: Thread, text: string, attachments: Attachment[], userId?: string) {
    const threadKey = normalizeThreadKey(thread.id);
    const input = await this.buildInput(text, attachments);
    const tracker = new ProgressTracker();
    const startTime = Date.now();

    log.info("execute_start", { thread_key: threadKey, user_id: userId });

    try {
      const sentMessage = await thread.post(this.streamTurn(threadKey, input, tracker, userId));

      const harness = (tracker as any).harness || "agent";
      const finalText = (tracker.resultText || tracker.lastAssistantText).trim();
      log.info("execute_complete", {
        thread_key: threadKey,
        duration_s: Math.round((Date.now() - startTime) / 100) / 10,
        result_length: finalText.length,
      });

      if (finalText) {
        const dur = (Date.now() - startTime) / 1000;
        const durStr = dur < 10 ? `${dur.toFixed(1)}s` : `${Math.round(dur)}s`;
        const hLabel = tracker.agentThreadId
          ? `[${harness}](https://ampcode.com/threads/${tracker.agentThreadId})`
          : harness;
        const meta = [process.env.APP_NAME || "Centaur", hLabel, durStr].filter(Boolean);
        let md = `_${meta.join(" · ")}_\n\n${finalText}`;
        if (this.viewerUrl) md += `\n\n[Thread Viewer](${this.viewerUrl}/${encodeURIComponent(threadKey)})`;
        try { await sentMessage.edit({ markdown: md }); } catch {}
      }

      if (finalText) {
        try {
          const slack = this.chat.getAdapter("slack") as SlackAdapter;
          const { channel, threadTs } = splitThreadKey(thread.id);
          await slack.setAssistantTitle(channel, threadTs, finalText.slice(0, 60));
        } catch {}
      }
    } catch (err) {
      if (err instanceof Error && err.message.includes("message_not_in_streaming_state")) {
        log.warn("slack_stream_expired", { thread_key: threadKey });
        await this.recoverExpired(thread, threadKey);
        return;
      }
      log.error("execute_error", { thread_key: threadKey, error: err instanceof Error ? err.message : String(err) });
      await thread.post(async function* () {
        yield { type: "task_update" as const, id: "init", title: "Failed", status: "error" as const };
        yield { type: "markdown_text" as const, text: formatError(err, "Agent request failed") };
      }());
    }
  }

  // ── Streaming ────────────────────────────────────────────────────────────

  private async *streamTurn(
    threadKey: string, input: string | InputContentBlock[], tracker: ProgressTracker, userId?: string,
  ): AsyncGenerator<StreamChunk> {
    if (this.viewerUrl) {
      yield { type: "markdown_text", text: `[Thread Viewer](${this.viewerUrl}/${encodeURIComponent(threadKey)})` };
    }
    yield { type: "task_update", id: "init", title: "Starting…", status: "in_progress" };

    for await (const event of this.client.execute({ threadKey, message: input, platform: "slack", userId })) {
      if (tracker.update(event)) {
        for (const chunk of tracker.pendingChunks()) yield chunk;
      }
    }

    if (!tracker.initCompleted) {
      yield { type: "task_update", id: "init", title: "Started", status: "complete" };
    }
  }

  // ── Helpers ──────────────────────────────────────────────────────────────

  private async loadAttachments(thread: Thread, msg: Message): Promise<Attachment[]> {
    if (msg.attachments?.length) return [...msg.attachments];
    const ts = (msg as { ts?: string }).ts || "";
    if (!ts) return [];
    try {
      const slack = this.chat.getAdapter("slack") as SlackAdapter;
      const refetched = await slack.fetchMessage(thread.id, ts);
      if (refetched?.attachments?.length) {
        log.info("mention_files_refetched", { thread: thread.id, count: refetched.attachments.length });
        return [...refetched.attachments];
      }
    } catch (err) {
      log.warn("mention_files_refetch_failed", { thread: thread.id, error: err instanceof Error ? err.message : String(err) });
    }
    return [];
  }

  private async buildInput(text: string, attachments: Attachment[]): Promise<string | InputContentBlock[]> {
    const blocks: InputContentBlock[] = [];
    for (const att of attachments) {
      if (!att.fetchData || !att.mimeType) continue;
      try {
        const data = await att.fetchData();
        const b64 = data.toString("base64");
        const source = { type: "base64" as const, media_type: att.mimeType, data: b64 };
        blocks.push(att.mimeType.startsWith("image/") ? { type: "image", source } : { type: "document", source });
      } catch (err) {
        log.warn("attachment_fetch_failed", { name: att.name || "unknown", error: err instanceof Error ? err.message : String(err) });
      }
    }
    return blocks.length > 0 ? [{ type: "text", text }, ...blocks] : text;
  }

  private async recoverExpired(thread: Thread, threadKey: string) {
    try {
      const status = await this.client.getStatus(threadKey);
      const result = typeof status.last_result === "string" ? status.last_result.trim() : "";
      if (result) {
        await thread.post({ markdown: result });
      } else if (this.viewerUrl) {
        await thread.post({ markdown: `Agent completed. [View full output](${this.viewerUrl}/${encodeURIComponent(threadKey)})` });
      }
    } catch {
      if (this.viewerUrl) {
        await thread.post({ markdown: `Agent completed. [View full output](${this.viewerUrl}/${encodeURIComponent(threadKey)})` });
      }
    }
  }
}

function formatError(err: unknown, context: string): string {
  if (err instanceof AxiosError) {
    const status = err.response?.status;
    if (!status) return `${context}: API is unreachable. Try again in ~30s.`;
    if (status >= 500) return `${context}: API returned ${status}. Try again shortly.`;
    return `${context}: ${err.message}`;
  }
  return `${context}: ${err instanceof Error ? err.message : "unknown error"}`;
}

// ── Singleton for Next.js ──────────────────────────────────────────────────

let _bot: SlackBot | null = null;
export function getBot(): SlackBot {
  if (!_bot) _bot = SlackBot.createFromEnv();
  return _bot;
}

export function getSlackBootstrapState() {
  return SlackBot.getBootstrapState();
}
