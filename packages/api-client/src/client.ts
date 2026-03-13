import type { CanonicalEvent } from "@centaur/harness-events";
import { EventSourceParserStream } from "eventsource-parser/stream";
import axios, { type AxiosInstance } from "axios";

export type InputContentBlock =
  | { type: "text"; text: string }
  | { type: "image"; source: { type: "base64"; media_type: string; data: string } }
  | { type: "document"; source: { type: "base64"; media_type: string; data: string } };

export interface ExecuteOptions {
  threadKey: string;
  message: string | InputContentBlock[];
  harness?: string;
  platform?: string;
  userId?: string;
  signal?: AbortSignal;
}

export interface PostContextOptions {
  threadKey: string;
  text: string;
  userId?: string;
  attachments?: Array<{ url: string; name: string; mimeType?: string }>;
}

export interface OrphanedEntry {
  thread_key: string;
  text: string;
  updated_at?: string | null;
}

const BUSY_MAX_RETRIES = 4;
const BUSY_INITIAL_DELAY_MS = 300;
const BUSY_MAX_DELAY_MS = 2500;

export class CentaurClient {
  readonly http: AxiosInstance;
  private log?: { info: Function; warn: Function; error: Function };

  constructor(opts: {
    apiUrl: string;
    apiKey: string;
    logger?: { info: Function; warn: Function; error: Function };
  }) {
    this.log = opts.logger;
    this.http = axios.create({
      baseURL: opts.apiUrl,
      headers: { Authorization: `Bearer ${opts.apiKey}` },
      timeout: 30_000,
    });
  }

  async *execute(opts: ExecuteOptions): AsyncGenerator<CanonicalEvent, void, undefined> {
    const { threadKey, message, harness, platform, userId, signal } = opts;

    for (let attempt = 1; attempt <= BUSY_MAX_RETRIES; attempt++) {
      this.log?.info("sse_connect", { thread_key: threadKey, harness });

      const body: Record<string, unknown> = { thread_key: threadKey, message };
      if (harness) body.harness = harness;
      if (platform) body.platform = platform;
      if (userId) body.user_id = userId;

      // SSE needs raw fetch — axios doesn't do streaming
      const res = await fetch(`${this.http.defaults.baseURL}/agent/execute`, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: (this.http.defaults.headers["Authorization"] ?? this.http.defaults.headers.common?.["Authorization"]) as string,
          "X-Trace-Id": threadKey,
        },
        body: JSON.stringify(body),
        signal,
      });

      if (!res.ok) {
        const text = await res.text().catch(() => "");
        let parsed: Record<string, unknown> | undefined;
        try { parsed = JSON.parse(text); } catch {}
        const code = parsed?.code as string | undefined;

        if (code === "THREAD_BUSY" && attempt < BUSY_MAX_RETRIES) {
          const delay = Math.min(BUSY_INITIAL_DELAY_MS * 2 ** (attempt - 1), BUSY_MAX_DELAY_MS);
          await new Promise((r) => setTimeout(r, delay));
          continue;
        }
        throw new Error(
          code
            ? `${code}: ${(parsed?.detail as string) ?? text.slice(0, 300)}`
            : `/agent/execute failed (${res.status}): ${text.slice(0, 300)}`,
        );
      }

      this.log?.info("sse_streaming", { thread_key: threadKey });
      if (!res.body) return;
      const stream = (res.body as ReadableStream<Uint8Array>)
        .pipeThrough(new TextDecoderStream())
        .pipeThrough(new EventSourceParserStream());
      for await (const event of stream) {
        if (event.data === "[DONE]") return;
        try { yield JSON.parse(event.data) as CanonicalEvent; } catch {}
      }
      return;
    }
  }

  async postContext(opts: PostContextOptions) {
    const { threadKey, text, userId, attachments } = opts;
    const metadata: Record<string, unknown> = {};
    if (userId) metadata.user_id = userId;
    if (attachments?.length) metadata.attachments = attachments;

    await this.http.post("/agent/messages", {
      thread_key: threadKey,
      messages: [{ role: "user", parts: [{ type: "text", text }], user_id: userId, metadata }],
    });
  }

  async getStatus(threadKey: string) {
    const { data } = await this.http.get("/agent/status", { params: { key: threadKey } });
    return data as Record<string, unknown>;
  }

  async listOrphaned(opts?: { maxAgeS?: number }) {
    const params = opts?.maxAgeS != null ? { max_age_s: opts.maxAgeS } : undefined;
    const { data } = await this.http.get("/agent/orphaned", { params });
    return data as OrphanedEntry[];
  }

  async claimDelivery(threadKey: string): Promise<boolean> {
    try {
      const { data } = await this.http.post("/agent/claim-delivery", { thread_key: threadKey });
      return data.claimed;
    } catch {
      return false;
    }
  }

  async markDelivered(threadKey: string) {
    await this.http.post("/agent/mark-delivered", { thread_key: threadKey });
  }
}
