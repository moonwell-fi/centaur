import * as crypto from "node:crypto";
import { Chat, parseMarkdown, type Root } from "chat";
import { generateId } from "ai";
import { createSlackAdapter } from "@chat-adapter/slack";
import { createPostgresState } from "@chat-adapter/state-pg";
import {
  extractArchiverSlackFiles,
  extractArchiverSource,
  extractRunOptions,
  fetchThreadContextMessages,
  fetchThreadRuntimeConfig,
  normalizeThreadKey,
  postThreadContextMessage,
  splitThreadKey,
  type ArchiverExtractResult,
  type BudgetMode,
  type Engine,
  type FileAttachment,
  type Harness,
} from "./harness";
import { log } from "@/lib/logger";
import { ApiError } from "./api-client";
import { executeStreamingWithBusyRetries, reconnectStreamingWithRetries } from "./modes";
import { truncateSlackText } from "./slack-text";
import {
  getThreadConfig,
  setThreadConfig as setThreadConfigRedis,
  type ThreadConfig,
} from "./thread-mode-store";
import { SlackLiveReply } from "./slack-live-reply";
import { ProgressTracker } from "./progress-tracker";
import { HandoffDetector } from "./handoff-detection";
import { resultToSlackMessages, type SlackReplyMetadata } from "./slack-blocks";
import { getPool } from "@/lib/db";

function formatErrorForSlack(error: unknown, context: string): string {
  if (error instanceof ApiError) {
    if (error.retryable && error.status === null) {
      return `${context}: API is unreachable (retried ${RETRY_DEFAULTS_MAX} times). The service may be restarting - try again in ~30s.`;
    }
    if (error.status && error.status >= 500) {
      return `${context}: API returned ${error.status}. The service may be overloaded - try again shortly.`;
    }
    return `${context}: ${error.message}`;
  }
  if (error instanceof Error) {
    return `${context}: ${error.message}`;
  }
  return `${context}: unknown error`;
}

const RETRY_DEFAULTS_MAX = 4;

const LOW_VALUE_PATTERNS = [
  /^i('ve| have) (handed off|delegated)/i,
  /^(handing off|delegating)/i,
  /^continuing in/i,
];

function isLowValueResult(text: string): boolean {
  if (!text) return true;
  return LOW_VALUE_PATTERNS.some((p) => p.test(text.trim()));
}

/**
 * Detect if text looks like a mid-thought that was cut off.
 * Used to trigger a reconnect attempt when the stream ended prematurely.
 */
function looksIncomplete(text: string): boolean {
  if (!text || text.length < 20) return false;
  const trimmed = text.trimEnd();
  // Ends with colon (about to do something), ellipsis, or "Let me ..."
  if (/:\s*$/.test(trimmed)) return true;
  if (/\.\.\.\s*$/.test(trimmed)) return true;
  if (/\blet me\b.{0,30}$/i.test(trimmed)) return true;
  if (/\bI'll\b.{0,30}$/i.test(trimmed)) return true;
  return false;
}

const THREAD_VIEWER_URL = process.env.THREAD_VIEWER_URL || "https://svc-ai.paradigm.xyz";
const MAX_TRACKED_MENTION_DELIVERIES = 5000;
const MENTION_DELIVERY_TTL_MS = 10 * 60 * 1000;
const SLACK_BOT_USERNAME = process.env.SLACK_BOT_USERNAME || "paradigm-ai";
const MAX_ARCHIVER_LINKS_PER_MESSAGE = 3;
const MAX_ARCHIVER_FILES_PER_MESSAGE = 5;

type MarkdownNode = Root | Root["children"][number];

const SLACK_BOT_TOKEN = process.env.SLACK_BOT_TOKEN || "";
const REQUIRED_SLACK_ENV_KEYS = ["SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET"] as const;

export function getSlackBootstrapState(): { ready: boolean; missingEnvKeys: string[] } {
  const missingEnvKeys = REQUIRED_SLACK_ENV_KEYS.filter((key) => {
    const value = process.env[key];
    return !value || value.trim().length === 0;
  });
  return {
    ready: missingEnvKeys.length === 0,
    missingEnvKeys: [...missingEnvKeys],
  };
}

function isPersonaHarness(harness: Harness): boolean {
  return harness === "legal" || harness === "eng" || harness === "invest" || harness === "events";
}

type SlackReply = {
  ts: string;
  user?: string;
  text?: string;
  bot_id?: string;
};

type SupportedSourceLink = {
  url: string;
  kind: "docsend" | "google_drive";
};

function collapseWhitespace(value: string): string {
  return value.replace(/\s+/g, " ").trim();
}

function clipText(value: string, maxChars = 360): string {
  return value.length > maxChars ? `${value.slice(0, maxChars)}...` : value;
}

function normalizeSharedUrl(raw: string): string {
  return raw
    .trim()
    .replace(/^<|>$/g, "")
    .replace(/[.,;:!?]+$/g, "");
}

function classifySupportedSource(rawUrl: string): SupportedSourceLink["kind"] | null {
  const normalized = rawUrl.toLowerCase();
  if (normalized.includes("docsend.com")) return "docsend";
  if (normalized.includes("docs.google.com") || normalized.includes("drive.google.com")) {
    return "google_drive";
  }
  return null;
}

function extractSupportedSourceLinks(text: string): SupportedSourceLink[] {
  const candidates = text.match(/https?:\/\/[^\s<>"'`)\]]+/gi) || [];
  const unique = new Set<string>();
  const links: SupportedSourceLink[] = [];
  for (const raw of candidates) {
    const url = normalizeSharedUrl(raw);
    if (!url || unique.has(url)) continue;
    const kind = classifySupportedSource(url);
    if (!kind) continue;
    unique.add(url);
    links.push({ url, kind });
  }
  return links;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function buildArchiverExtractionContext(
  source: SupportedSourceLink,
  payload: ArchiverExtractResult,
): string {
  const status = readString(payload.status) || "unknown";
  const error = readString(payload.error);
  const files = Array.isArray(payload.files) ? payload.files : [];
  const successful = files.filter(
    (file) =>
      file &&
      typeof file === "object" &&
      readString(file.status) === "ok",
  );
  const lines: string[] = [
    "## Source Ingestion (archiver)",
    `- URL: ${source.url}`,
    `- Source type: ${source.kind}`,
    `- Status: ${status}`,
    `- Parsed files: ${successful.length}/${files.length}`,
  ];
  if (error) {
    lines.push(`- Error: ${error}`);
  }
  for (const file of successful.slice(0, 2)) {
    const fileMeta = file.file || {};
    const metadata = (file.metadata as Record<string, unknown> | undefined) || {};
    const filename = readString(fileMeta.filename) || "unknown";
    const company = readString(
      (metadata.company as Record<string, unknown> | undefined)?.name,
    );
    const docType = readString(
      (metadata.document as Record<string, unknown> | undefined)?.doc_type,
    );
    const oneLiner = readString(
      (metadata.summary as Record<string, unknown> | undefined)?.one_liner,
    );
    const parsedText = readString(file.parsed_text);
    lines.push(
      `- File: ${filename}` +
      (company ? ` | company=${company}` : "") +
      (docType ? ` | type=${docType}` : ""),
    );
    if (oneLiner) {
      lines.push(`- Metadata summary: ${clipText(collapseWhitespace(oneLiner), 220)}`);
    }
    if (parsedText) {
      lines.push(`- Parsed excerpt: ${clipText(collapseWhitespace(parsedText), 320)}`);
    }
  }
  if (status !== "ok") {
    lines.push(
      "- Note: If link is auth-gated, ask user for required email/password or direct file upload.",
    );
  }
  return lines.join("\n");
}

function buildArchiverFilesContext(
  files: FileAttachment[],
  payload: ArchiverExtractResult,
): string {
  const status = readString(payload.status) || "unknown";
  const error = readString(payload.error);
  const parsedFiles = Array.isArray(payload.files) ? payload.files : [];
  const successful = parsedFiles.filter(
    (file) =>
      file &&
      typeof file === "object" &&
      readString(file.status) === "ok",
  );
  const lines: string[] = [
    "## Uploaded Materials (archiver)",
    `- Uploaded files: ${files.map((file) => file.name).join(", ")}`,
    `- Status: ${status}`,
    `- Parsed files: ${successful.length}/${parsedFiles.length}`,
  ];
  if (error) {
    lines.push(`- Error: ${error}`);
  }
  for (const file of successful.slice(0, 3)) {
    const fileMeta = file.file || {};
    const metadata = (file.metadata as Record<string, unknown> | undefined) || {};
    const filename = readString(fileMeta.filename) || "unknown";
    const docType = readString(
      (metadata.document as Record<string, unknown> | undefined)?.doc_type,
    );
    const oneLiner = readString(
      (metadata.summary as Record<string, unknown> | undefined)?.one_liner,
    );
    const parsedText = readString(file.parsed_text);
    lines.push(
      `- File: ${filename}` + (docType ? ` | type=${docType}` : ""),
    );
    if (oneLiner) {
      lines.push(`- Metadata summary: ${clipText(collapseWhitespace(oneLiner), 220)}`);
    }
    if (parsedText) {
      lines.push(`- Parsed excerpt: ${clipText(collapseWhitespace(parsedText), 320)}`);
    }
  }
  if (status !== "ok") {
    lines.push(
      "- Note: If parsing failed or the file is unavailable, ask the user for a shareable link or extracted text.",
    );
  }
  return lines.join("\n");
}

async function ingestSupportedLinksIntoThreadContext(params: {
  threadKey: string;
  sourceLinks: SupportedSourceLink[];
  userId?: string;
  messageIdBase: string;
  timeoutMs?: number;
  maxLinks?: number;
}): Promise<void> {
  const limit = params.maxLinks ?? MAX_ARCHIVER_LINKS_PER_MESSAGE;
  const limited = params.sourceLinks.slice(0, limit);
  if (limited.length === 0) return;
  const startedAt = Date.now();
  let successCount = 0;
  let errorCount = 0;
  for (let index = 0; index < limited.length; index += 1) {
    const source = limited[index];
    const contextMessageId = `${params.messageIdBase}:archiver:${index + 1}`;
    try {
      const payload = await extractArchiverSource(source.url, {
        maxDepth: 2,
        context: {
          thread_key: params.threadKey,
          source_kind: source.kind,
          source: "slack_subscribed_message",
        },
        timeoutMs: params.timeoutMs ?? 180_000,
      });
      const contextText = buildArchiverExtractionContext(source, payload);
      await postThreadContextMessage(params.threadKey, contextText, {
        source: "slack_archiver_ingest",
        userId: params.userId,
        messageId: contextMessageId,
      });
      successCount += 1;
    } catch (error) {
      const err = error instanceof Error ? error.message : String(error);
      await postThreadContextMessage(
        params.threadKey,
        [
          "## Source Ingestion (archiver)",
          `- URL: ${source.url}`,
          `- Source type: ${source.kind}`,
          "- Status: error",
          `- Error: ${err}`,
          "- Note: If link is auth-gated, ask user for required email/password or direct file upload.",
        ].join("\n"),
        {
          source: "slack_archiver_ingest",
          userId: params.userId,
          messageId: contextMessageId,
        },
      );
      errorCount += 1;
    }
  }
  console.info("archiver_context_ingest_complete", {
    thread: params.threadKey,
    attempted: limited.length,
    succeeded: successCount,
    failed: errorCount,
    elapsed_ms: Date.now() - startedAt,
  });
}

async function ingestSlackFilesIntoThreadContext(params: {
  threadKey: string;
  files: FileAttachment[];
  userId?: string;
  messageIdBase: string;
  slackTs?: string;
  timeoutMs?: number;
  maxFiles?: number;
}): Promise<string> {
  const limit = params.maxFiles ?? MAX_ARCHIVER_FILES_PER_MESSAGE;
  const files = params.files.slice(0, limit);
  if (files.length === 0) return "";
  const payload = await extractArchiverSlackFiles(files, {
    context: {
      thread_key: params.threadKey,
      source: "slack_upload",
    },
    timeoutMs: params.timeoutMs ?? 180_000,
  });
  const contextText = buildArchiverFilesContext(files, payload);
  await postThreadContextMessage(params.threadKey, contextText, {
    source: "slack_archiver_ingest",
    userId: params.userId,
    messageId: `${params.messageIdBase}:archiver-files`,
    slackTs: params.slackTs,
    attachments: files,
  });
  return contextText;
}

async function fetchThreadHistory(
  channel: string,
  threadTs: string,
  botUserId?: string,
): Promise<string> {
  if (!SLACK_BOT_TOKEN) return "";
  try {
    const params = new URLSearchParams({
      channel,
      ts: threadTs,
      limit: "50",
      inclusive: "true",
    });
    const res = await fetch(
      `https://slack.com/api/conversations.replies?${params}`,
      { headers: { Authorization: `Bearer ${SLACK_BOT_TOKEN}` } },
    );
    const data = (await res.json()) as {
      ok: boolean;
      messages?: SlackReply[];
    };
    if (!data.ok || !data.messages || data.messages.length <= 1) return "";

    const prior = data.messages.slice(0, -1).filter((m) => {
      if (m.bot_id) return false;
      if (botUserId && m.user === botUserId) return false;
      return true;
    });
    if (prior.length === 0) return "";

    const lines = prior.map((m) => {
      const user = m.user ? `<@${m.user}>` : "Unknown";
      return `${user}: ${m.text || "(no text)"}`;
    });

    return [
      "## Prior Thread Messages",
      "",
      "The following messages were posted in this Slack thread before you were mentioned. Use them as context:",
      "",
      ...lines,
      "",
      "---",
      "",
    ].join("\n");
  } catch (error) {
    log.warn("fetch_thread_history_failed", {
      channel,
      threadTs,
      error: error instanceof Error ? error.message : String(error),
    });
    return "";
  }
}

function messageIdentifier(message: {
  ts?: string;
  userId?: string;
  text?: string;
  threadId?: string;
}): string {
  const ts = String(message.ts || "").trim();
  if (ts) return ts;
  const raw = `${message.threadId || ""}:${message.userId || ""}:${message.text || ""}`;
  return crypto.createHash("sha1").update(raw).digest("hex");
}

function preprocessSlackLinks(text: string): string {
  let result = text;
  result = result.replace(/&lt;(https?:\/\/[^|&]+)\|([^&]+)&gt;/g, "[$2]($1)");
  result = result.replace(/<(https?:\/\/[^|>]+)\|([^>]+)>/g, "[$2]($1)");
  return result;
}

function preprocessMarkdownTables(text: string): string {
  const lines = text.split("\n");
  const result: string[] = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];
    if (/^\s*\|.*\|.*\|\s*$/.test(line)) {
      const tableLines: string[] = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) {
        tableLines.push(lines[i]);
        i++;
      }

      const parseRow = (row: string): string[] =>
        row
          .replace(/^\s*\|/, "")
          .replace(/\|\s*$/, "")
          .split("|")
          .map((c) => c.trim());

      const dataRows = tableLines.filter(
        (l) => !/^\s*\|[\s:|-]+\|\s*$/.test(l)
      );
      if (dataRows.length === 0) {
        result.push(...tableLines);
        continue;
      }

      const headers = parseRow(dataRows[0]);
      const bodyRows = dataRows.slice(1);

      if (bodyRows.length === 0) {
        result.push(headers.map((h) => `*${h}*`).join("  ·  "));
        result.push("");
      } else {
        for (const row of bodyRows) {
          const cells = parseRow(row);
          const label = cells[0] || "";
          result.push(`*${label}*`);
          for (let c = 1; c < cells.length; c++) {
            const headerLabel = headers[c] || `Col ${c}`;
            result.push(`• *${headerLabel}:* ${cells[c] || "—"}`);
          }
          result.push("");
        }
      }
    } else {
      result.push(line);
      i++;
    }
  }

  return result.join("\n");
}

function renderSlackMessage(markdown: string) {
  const ast = parseMarkdown(preprocessMarkdownTables(preprocessSlackLinks(markdown)));
  const escapeLiteralTildes = (
    node: MarkdownNode,
    inDelete = false
  ): void => {
    const insideDelete = inDelete || node.type === "delete";

    if (node.type === "text" && !insideDelete) {
      node.value = node.value.replace(/~/g, "\\~");
    }

    if ("children" in node && Array.isArray(node.children)) {
      for (const child of node.children as Root["children"]) {
        escapeLiteralTildes(child, insideDelete);
      }
    }
  };

  escapeLiteralTildes(ast);

  return { ast };
}

function toSlackMessage(markdown: string) {
  return renderSlackMessage(truncateSlackText(markdown));
}


function createBot() {
  const hasSlackCreds =
    process.env.SLACK_BOT_TOKEN && process.env.SLACK_SIGNING_SECRET;

  const bot = new Chat({
    userName: SLACK_BOT_USERNAME,
    adapters: hasSlackCreds ? { slack: createSlackAdapter() } : {},
    state: createPostgresState({ client: getPool() }),
    onLockConflict: "force",
  } as unknown as ConstructorParameters<typeof Chat>[0]);
  const recentMentionDeliveries = new Map<string, number>();

  function claimMentionDelivery(
    threadId: string,
    message: { ts?: string; id?: string },
  ): boolean {
    const ts = String(message.ts || "").trim();
    const deliveryId = ts || String(message.id || "").trim();
    if (!deliveryId) return true;
    const threadKey = normalizeThreadKey(threadId);
    const claimKey = `${threadKey}:${deliveryId}`;
    const now = Date.now();

    for (const [key, seenAt] of recentMentionDeliveries) {
      if (now - seenAt > MENTION_DELIVERY_TTL_MS) {
        recentMentionDeliveries.delete(key);
      }
    }

    if (recentMentionDeliveries.has(claimKey)) {
      return false;
    }

    if (recentMentionDeliveries.size >= MAX_TRACKED_MENTION_DELIVERIES) {
      const oldestKey = recentMentionDeliveries.keys().next().value as string | undefined;
      if (oldestKey) recentMentionDeliveries.delete(oldestKey);
    }

    recentMentionDeliveries.set(claimKey, now);
    return true;
  }

  function setThreadConfig(threadKey: string, config: ThreadConfig): void {
    setThreadConfigRedis(threadKey, config).catch((err) => {
      log.warn("setThreadConfig_redis_failed", { threadKey, error: String(err) });
    });
  }

  function buildSessionContext(threadId: string, requesterUserId?: string): string {
    const now = new Date().toISOString().replace("T", " ").slice(0, 19);
    return [
      "# Session Context",
      "",
      `- **Date/Time**: ${now} UTC`,
      `- **Thread ID**: ${threadId}`,
      `- **Platform**: Slack`,
      "",
      "## Slack Formatting Rules",
      "",
      "- Use standard markdown links `[Display Text](URL)` for hyperlinks",
      "- Do NOT use Slack-native `<URL|text>` link syntax",
      "- Preserve Slack user mentions (`<@UXXXXXXX>`) exactly as-is — only use these for actual Slack users",
      "- For Twitter/X handles, link to the profile: `[@handle](https://x.com/handle)`",
      "- Prefer concise, well-structured markdown; long replies may be split across multiple Slack messages",
      "- Markdown tables are allowed and may render as native Slack tables when the structure is clean",
      requesterUserId
        ? `- After completing a long task, tag the requester with their real Slack mention: <@${requesterUserId}>`
        : "- After completing a long task, tag the requester with their real Slack mention if available",
      "",
      "---",
      "",
    ].join("\n");
  }

  async function handleMessage(
    thread: Parameters<Parameters<typeof bot.onNewMention>[0]>[0],
    messageText: string,
    isFirstMessage: boolean,
    attachments?: Array<{ url?: string; name?: string }>,
    userId?: string,
    slackTs?: string,
  ) {
    const requestId = generateId();
    const rawThreadKey = thread.id;
    const threadKey = normalizeThreadKey(rawThreadKey);
    const previous = await getThreadConfig(threadKey);
    const files: FileAttachment[] = (attachments || [])
      .filter((a): a is { url: string; name: string } => !!a.url && !!a.name)
      .map((a) => ({ url: a.url, name: a.name }));

    let recovered: {
      harness: Harness | null;
      engine: Engine | null;
    } | null = null;
    if (!isFirstMessage && !previous) {
      try {
        recovered = await fetchThreadRuntimeConfig(threadKey);
      } catch (error) {
        log.warn("thread_runtime_config_recovery_failed", {
          thread: threadKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
    }

    const activeHarness = previous?.harness ?? recovered?.harness ?? null;
    const activeEngine = previous?.engine ?? recovered?.engine ?? null;
    const parsed = extractRunOptions(messageText, { activeHarness });
    const harness: Harness = isFirstMessage ? parsed.harness : (activeHarness ?? parsed.harness);
    const engine = parsed.engine ?? activeEngine ?? null;
    const model = parsed.model ?? previous?.model ?? null;
    const budgetMode = parsed.budgetMode ?? previous?.budgetMode ?? null;

    if (!isFirstMessage && !activeHarness && !parsed.harnessExplicit) {
      await thread.post(
        toSlackMessage(
          "I could not recover the active harness for this thread. Please retry with an explicit harness flag (for example `--legal` or `--invest`)."
        )
      );
      return;
    }

    if (
      !isFirstMessage &&
      activeHarness &&
      parsed.harnessExplicit &&
      parsed.harness !== activeHarness
    ) {
      await thread.post(
        toSlackMessage(
          "This thread is already running with a different harness. Start a new thread to switch."
        )
      );
      return;
    }
    if (
      !isFirstMessage &&
      activeEngine &&
      parsed.engineExplicit &&
      parsed.engine &&
      parsed.engine !== activeEngine
    ) {
      await thread.post(
        toSlackMessage(
          "This thread is already running with a different engine. Start a new thread to switch."
        )
      );
      return;
    }

    if (!parsed.cleanedText && !isPersonaHarness(harness)) {
      await thread.post(
        toSlackMessage(
          "Please provide a prompt after flags. Example: `--amp build me a dashboard`."
        )
      );
      return;
    }

    const instruction = parsed.cleanedText || "hey";

    setThreadConfig(threadKey, {
      harness,
      engine,
      model,
      budgetMode,
    });

    try {
      await thread.startTyping("Running...");
      let threadHistory = "";
      const { channel, threadTs } = splitThreadKey(threadKey);
      if (isFirstMessage) {
        threadHistory = await fetchThreadHistory(channel, threadTs);
      }
      let persistedContext = "";
      try {
        const storedContexts = await fetchThreadContextMessages(threadKey, {
          sources: ["slack_archiver_ingest"],
          limit: 6,
        });
        if (storedContexts.length > 0) {
          persistedContext = [
            "## Previously Extracted Materials",
            "",
            ...storedContexts,
            "",
            "---",
            "",
          ].join("\n");
        }
      } catch (error) {
        log.warn("thread_context_fetch_failed", {
          thread: threadKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }

      let message = instruction;
      if (isFirstMessage) {
        const contextPrefix = buildSessionContext(threadKey, userId);
        message = contextPrefix + threadHistory + persistedContext + instruction;
      } else if (persistedContext) {
        message = `${persistedContext}${instruction}`;
      }

      if (budgetMode) {
        message = `[budget: ${budgetMode}]\n\n${message}`;
      }

      const viewerUrl = `${THREAD_VIEWER_URL}/${encodeURIComponent(normalizeThreadKey(threadKey))}`;
      const liveReply = new SlackLiveReply(channel, threadTs);
      await liveReply.start("⏳ Working...", { viewerUrl });
      const tracker = new ProgressTracker();
      if (files.length > 0 && isPersonaHarness(harness)) {
        try {
          await thread.startTyping("Reading shared files...");
          const fileContext = await ingestSlackFilesIntoThreadContext({
            threadKey,
            files,
            userId,
            messageIdBase: requestId,
            slackTs,
            timeoutMs: 180_000,
          });
          if (fileContext) {
            message = `${message}\n\n${fileContext}`;
          }
        } catch (error) {
          log.warn("slack_file_ingest_sync_failed", {
            thread: threadKey,
            error: error instanceof Error ? error.message : String(error),
          });
          message = [
            message,
            "",
            "Shared Slack uploads were present, but I could not parse them yet.",
            "If the files are important to the call, ask the user for a shareable link or extracted text.",
          ].join("\n");
        }
      }
      const sourceLinks = extractSupportedSourceLinks(instruction);
      if (sourceLinks.length > 0 && isPersonaHarness(harness)) {
        const immediateLinks = sourceLinks.slice(0, 1);
        let syncLinkIngestSucceeded = false;
        try {
          await thread.startTyping("Extracting shared materials...");
          console.info("source_link_ingest_sync_start", {
            thread: threadKey,
            harness,
            request_id: requestId,
            link_count: immediateLinks.length,
          });
          await ingestSupportedLinksIntoThreadContext({
            threadKey,
            sourceLinks: immediateLinks,
            userId,
            messageIdBase: `${requestId}:ingest`,
            timeoutMs: 60_000,
            maxLinks: 1,
          });
          syncLinkIngestSucceeded = true;
        } catch (error) {
          console.warn("source_link_ingest_sync_failed", {
            thread: threadKey,
            error: error instanceof Error ? error.message : String(error),
          });
        }

        const deferredLinks = sourceLinks.slice(1);
        if (deferredLinks.length > 0) {
          console.info("source_link_ingest_deferred_start", {
            thread: threadKey,
            harness,
            request_id: requestId,
            link_count: deferredLinks.length,
          });
          void ingestSupportedLinksIntoThreadContext({
            threadKey,
            sourceLinks: deferredLinks,
            userId,
            messageIdBase: `${requestId}:ingest-deferred`,
            timeoutMs: 120_000,
          }).catch((error) => {
            console.warn("source_link_ingest_deferred_failed", {
              thread: threadKey,
              error: error instanceof Error ? error.message : String(error),
            });
          });
        }

        const sourceContext = [
          "Detected source links (DocSend/Google Drive):",
          ...sourceLinks.map((link) => `- ${link.url} (${link.kind})`),
          "",
          "Extract each link using: `call archiver extract_source '{\"source_url\":\"<url>\",\"output_dir\":\"/tmp/archiver/<company>\"}'`",
          "If extraction fails due to auth, ask the user for the required email/password or a direct file upload.",
          syncLinkIngestSucceeded
            ? `Pre-ingested ${Math.min(1, sourceLinks.length)} link as thread context; ${deferredLinks.length > 0 ? `${deferredLinks.length} more ingesting in background.` : "all links covered."}`
            : "Automatic pre-ingest did not complete yet. If the first extraction matters immediately, run `archiver.extract_source` manually or ask the user for auth details.",
        ].join("\n");
        message = `${message}\n\n${sourceContext}`;
      }
      const executionStartedAt = Date.now();
      let finalMessage = "";

      try {
        let streamReturn = "";
        // Track total events yielded across iterations so reconnect can skip
        // already-seen events (the API replays full stdout history on reconnect).
        let totalYieldedCount = 0;

        // Phase 1: initial execute — sends turn.start to the container.
        {
          const handoffDetector = new HandoffDetector();
          let detectedHandoff = false;

          const gen = executeStreamingWithBusyRetries({
            threadKey,
            message,
            harness,
            engine,
          });

          while (true) {
            const { done, value } = await gen.next();
            if (done) {
              if (!detectedHandoff) streamReturn = value || "";
              break;
            }
            if (detectedHandoff) continue;

            totalYieldedCount++;
            if (tracker.update(value)) {
              liveReply.queueUpdate(tracker.toSlackBullets());
            }

            const handoff = handoffDetector.processEvent(value);
            if (handoff && handoff.follow) {
              tracker.addHandoff(handoff.goal, handoff.newThreadKey);
              liveReply.queueUpdate(tracker.toSlackBullets());
              detectedHandoff = true;
            }
          }

          // Phase 2: follow handoff chain via reconnect (no turn.start).
          // After follow=true, Amp navigates to the new thread and continues
          // autonomously. We reconnect to the same container to read its output
          // instead of sending a new turn.start which would create a competing
          // turn and produce a stale "mid-reply" summary.
          while (detectedHandoff) {
            detectedHandoff = false;
            const nextHandoffDetector = new HandoffDetector();

            const reconnGen = reconnectStreamingWithRetries({
              threadKey,
              harness,
              engine,
              skipCount: totalYieldedCount,
            });

            while (true) {
              const { done, value } = await reconnGen.next();
              if (done) {
                if (!detectedHandoff) streamReturn = value || "";
                break;
              }
              if (detectedHandoff) continue;

              totalYieldedCount++;
              if (tracker.update(value)) {
                liveReply.queueUpdate(tracker.toSlackBullets());
              }

              const handoff = nextHandoffDetector.processEvent(value);
              if (handoff && handoff.follow) {
                tracker.addHandoff(handoff.goal, handoff.newThreadKey);
                liveReply.queueUpdate(tracker.toSlackBullets());
                detectedHandoff = true;
              }
            }
          }
        }

        // Phase 3: incomplete-result recovery.
        // If we got no proper result event and the last assistant text looks
        // like a mid-thought (ends with colon, "Let me", etc.), the stream
        // may have ended prematurely. Try a single reconnect to capture any
        // remaining output from a still-running container.
        const prelimResult = (tracker.resultText || tracker.lastAssistantText || streamReturn).trim();
        if (!tracker.resultText && looksIncomplete(prelimResult)) {
          try {
            const recoveryGen = reconnectStreamingWithRetries({
              threadKey,
              harness,
              engine,
              skipCount: totalYieldedCount,
            });
            while (true) {
              const { done, value } = await recoveryGen.next();
              if (done) {
                if (value) streamReturn = value;
                break;
              }
              totalYieldedCount++;
              if (tracker.update(value)) {
                liveReply.queueUpdate(tracker.toSlackBullets());
              }
            }
          } catch {
            // Recovery is best-effort — don't fail the whole request
          }
        }
        finalMessage = (tracker.resultText || tracker.lastAssistantText || streamReturn).trim();
      } catch (error) {
        liveReply.dispose();
        throw error;
      }

      // Persist user + assistant messages to chat_messages for thread viewer.
      // Use the Slack message timestamp for the user message so it sorts in
      // the original conversation order. The assistant gets +1ms to sort after.
      try {
        const pool = getPool();
        const dbClient = await pool.connect();
        try {
          const slackEpoch = slackTs ? parseFloat(slackTs) : 0;
          const userEpochMs = slackEpoch > 1_000_000_000 ? Math.floor(slackEpoch * 1000) : Date.now();
          const userMsgId = `slack-user-${threadKey}-${userEpochMs}`;
          const assistantMsgId = `slack-asst-${threadKey}-${userEpochMs + 1}`;
          const userTs = new Date(userEpochMs).toISOString();
          const assistantTs = new Date(userEpochMs + 1).toISOString();
          await dbClient.query("BEGIN");
          await dbClient.query(
            `INSERT INTO chat_messages (id, thread_key, role, parts, metadata, created_at)
             VALUES ($1, $2, 'user', $3::jsonb, $4::jsonb, $5::timestamptz)
             ON CONFLICT (id) DO NOTHING`,
            [
              userMsgId,
              threadKey,
              JSON.stringify([{ type: "text", text: instruction }]),
              JSON.stringify({ harness, ...(engine ? { engine } : {}) }),
              userTs,
            ],
          );
          if (finalMessage) {
            await dbClient.query(
              `INSERT INTO chat_messages (id, thread_key, role, parts, metadata, created_at)
               VALUES ($1, $2, 'assistant', $3::jsonb, $4::jsonb, $5::timestamptz)
               ON CONFLICT (id) DO NOTHING`,
              [
                assistantMsgId,
                threadKey,
                JSON.stringify([{ type: "text", text: finalMessage }]),
                JSON.stringify({ harness, thread_name: finalMessage.slice(0, 60) }),
                assistantTs,
              ],
            );
          }
          await dbClient.query("COMMIT");
        } catch {
          await dbClient.query("ROLLBACK");
        } finally {
          dbClient.release();
        }
      } catch {
        // Best-effort — don't block Slack reply
      }

      // Single Slack message — edit the live reply in-place with the final result.
      if (isLowValueResult(finalMessage)) {
        await liveReply.finish(tracker.toSlackBullets());
      } else {
        const metadata: SlackReplyMetadata = {
          threadKey: normalizeThreadKey(threadKey),
          harness,
          durationSeconds: Math.max(0, (Date.now() - executionStartedAt) / 1000),
          toolCount: tracker.completedTools.length,
          tokenCount: tracker.usage.totalTokens,
          costUsd: tracker.usage.costUsd > 0 ? tracker.usage.costUsd : null,
          usageEstimated: tracker.usage.costUsd <= 0,
          sourceLabel: "Paradigm AI",
        };
        const payloads = resultToSlackMessages(finalMessage, metadata);
        if (payloads.length > 0) {
          await liveReply.finishRich(payloads);
        } else {
          await liveReply.finish(tracker.toSlackBullets());
        }
      }
    } catch (error) {
      await thread.post(
        toSlackMessage(formatErrorForSlack(error, "Agent request failed"))
      );
    }
  }

  bot.onNewMention(async (thread, message) => {
    if (message.author.isMe) return;
    if (message.author.isBot) return;
    if (!claimMentionDelivery(thread.id, {
      ts: (message as { ts?: string }).ts,
      id: (message as { id?: string }).id,
    })) {
      log.info("duplicate_mention_ignored", {
        thread: normalizeThreadKey(thread.id),
        handler: "onNewMention",
        ts: (message as { ts?: string }).ts || "",
      });
      return;
    }
    await thread.subscribe();
    const attachments = message.attachments?.map((a) => ({ url: a.url, name: a.name }));
    const mentionTs = (message as { ts?: string }).ts || "";
    await handleMessage(thread, message.text, true, attachments, message.author.userId, mentionTs);
  });

  bot.onSubscribedMessage(async (thread, message) => {
    if (message.author.isMe) return;
    if (message.author.isBot) return;
    const attachments = message.attachments?.map((a) => ({ url: a.url, name: a.name }));
    if (!message.isMention) {
      const text = (message.text || "").trim();
      const threadKey = normalizeThreadKey(thread.id);
      const threadConfig = await getThreadConfig(threadKey);
      let activeHarness: Harness | null = threadConfig?.harness ?? null;
      const files: FileAttachment[] = (attachments || [])
        .filter((a): a is { url: string; name: string } => !!a.url && !!a.name)
        .map((a) => ({ url: a.url, name: a.name }));
      if (!text && files.length === 0) return;
      const messageId = messageIdentifier({
        ts: (message as { ts?: string }).ts || (message as { id?: string }).id,
        userId: message.author.userId,
        text,
        threadId: thread.id,
      });

      const contextText = text || "Shared attachment in thread.";
      const slackTs = (message as { ts?: string }).ts || "";
      try {
        await postThreadContextMessage(threadKey, contextText, {
          source: "slack_subscribed_message",
          userId: message.author.userId,
          messageId,
          slackTs,
          attachments: files.length > 0 ? files : undefined,
        });
      } catch (error) {
        log.warn("thread_context_post_failed", {
          thread: threadKey,
          error: error instanceof Error ? error.message : String(error),
        });
      }
      if (!activeHarness) {
        try {
          const recovered = await fetchThreadRuntimeConfig(threadKey);
          activeHarness = recovered.harness;
          if (recovered.harness) {
            setThreadConfig(threadKey, {
              harness: recovered.harness,
              engine: recovered.engine,
              model: null,
              budgetMode: null,
            });
          }
        } catch (error) {
          console.warn("thread_runtime_config_recovery_failed_subscribed", {
            thread: threadKey,
            error: error instanceof Error ? error.message : String(error),
          });
        }
      }
      if (activeHarness && isPersonaHarness(activeHarness as Harness) && text) {
        const sourceLinks = extractSupportedSourceLinks(text);
        if (sourceLinks.length > 0) {
          void ingestSupportedLinksIntoThreadContext({
            threadKey,
            sourceLinks,
            userId: message.author.userId,
            messageIdBase: messageId,
          }).catch((error) => {
            console.warn("archiver_context_ingest_failed", {
              thread: threadKey,
              error: error instanceof Error ? error.message : String(error),
            });
          });
        }
      }
      if (activeHarness && isPersonaHarness(activeHarness as Harness) && files.length > 0) {
        void ingestSlackFilesIntoThreadContext({
          threadKey,
          files,
          userId: message.author.userId,
          messageIdBase: messageId,
          slackTs,
        }).catch((error) => {
          log.warn("slack_file_ingest_deferred_failed", {
            thread: threadKey,
            error: error instanceof Error ? error.message : String(error),
          });
        });
      }
      return;
    }
    if (!claimMentionDelivery(thread.id, {
      ts: (message as { ts?: string }).ts,
      id: (message as { id?: string }).id,
    })) {
      log.info("duplicate_mention_ignored", {
        thread: normalizeThreadKey(thread.id),
        handler: "onSubscribedMessage",
        ts: (message as { ts?: string }).ts || "",
      });
      return;
    }
    const subTs = (message as { ts?: string }).ts || "";
    await handleMessage(thread, message.text, false, attachments, message.author.userId, subTs);
  });

  return bot;
}

let _bot: ReturnType<typeof createBot> | null = null;
export function getBot() {
  if (!_bot) _bot = createBot();
  return _bot;
}
