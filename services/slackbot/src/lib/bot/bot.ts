import { normalizeThreadKey, splitThreadKey } from "@centaur/harness-events";
import type { CanonicalEvent } from "@centaur/harness-events";
import { CentaurClient } from "@centaur/api-client";
import type { InputContentBlock } from "@centaur/api-client";

import { log } from "@/lib/logger";
import {
  stringifyMarkdown,
  renderMarkdownForSlack,
  type Root,
} from "@/lib/slack/markdown";
import { classifySlackError } from "@/lib/slack/errors";
import {
  flattenMarkdownTables,
  isCancellationTerminalState,
  isSlackInvalidBlocksError,
  normalizedTerminalString,
  renderTerminalResultCopy,
  splitSlackMessage,
} from "@/lib/slack/delivery";
import type { StreamChunk } from "@/lib/slack/types";
import { ProgressTracker } from "./progress-tracker";
import { convertDashboardBlocks } from "./dashboard-to-slack";

const KEEPALIVE_MS = 120_000; // 2 min — Slack expires streaming state after ~5 min
const STREAM_EXPIRED_POLL_INTERVAL_MS = 3_000;
const STREAM_EXPIRED_POLL_MAX_MS = 5 * 60_000;
const RECONNECT_MAX_RETRIES = 3;
const RECONNECT_BASE_DELAY_MS = 2_000;
const FINAL_DELIVERY_BATCH_SIZE = 5;
const FINAL_DELIVERY_IDLE_MS = 2_000;
const FINAL_DELIVERY_ERROR_MS = 5_000;
const FINAL_DELIVERY_LEASE_SECONDS = 90;
const EXECUTION_HARNESSES = new Set(["amp", "claude-code", "codex", "pi-mono"]);
const PROMPT_FLAG_ALIASES = new Map<string, string>([
  ["claude", "claude-code"],
  ["pi", "pi-mono"],
]);
const STREAM_BOOTSTRAP_TEXT = "\u200b";

export { splitSlackMessage } from "@/lib/slack/delivery";

type SlackRawMessage = {
  team_id?: string;
  team?: string;
  ts?: string;
};

type DeliveryContext = {
  messageId?: string;
  userId?: string;
  teamId?: string;
};

type InFlightExecution = {
  executionId: string;
  abortController: AbortController;
};

type FinalDeliveryRecord = {
  execution_id?: string;
  thread_key?: string;
  attempt_count?: number;
  delivery?: Record<string, unknown>;
  final_payload?: Record<string, unknown> | null;
};

const PROMPT_FLAG_RE = /(?:^|\s)--([a-z][a-z0-9-]*)(?=\s|$)/gi;
const PROMPT_FLAG_SKIP = new Set(["engine", "model", "opus", "sonnet", "haiku"]);

/**
 * Extract every `--flag` token. Returns the last matched selector (persona or
 * harness) plus the text with all flag tokens stripped so the LLM never sees
 * `--invest` in the prompt body. Persona/harness routing is flag-only by design:
 * never infer persona from channel, content, or attachments.
 */
export function extractFlagSelector(text: string): { selector?: string; cleaned: string } {
  const re = new RegExp(PROMPT_FLAG_RE.source, PROMPT_FLAG_RE.flags);
  let selector: string | undefined;
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    const flag = match[1].toLowerCase();
    if (!PROMPT_FLAG_SKIP.has(flag)) {
      selector = PROMPT_FLAG_ALIASES.get(flag) || flag;
    }
  }
  const cleaned = text
    .replace(new RegExp(PROMPT_FLAG_RE.source, PROMPT_FLAG_RE.flags), " ")
    .replace(/\s+/g, " ")
    .trim();
  return { selector, cleaned };
}

/** Backwards-compat wrapper: the raw selector string only. */
export function parsePromptSelectorFlag(text: string): string | undefined {
  return extractFlagSelector(text).selector;
}

/**
 * Strip residual mention tokens (`<@U...>`) so we can detect a truly empty
 * payload after flag removal. Used by the bare-trigger short-circuit.
 */
function stripMentions(text: string): string {
  return text.replace(/<@[A-Z0-9]+>/g, "").replace(/\s+/g, " ").trim();
}

/**
 * Persona-specific canned responses for bare-flag invocations (e.g. just
 * `@centaur_ai --invest`). The LLM's compliance with prompt-level "respond
 * with exactly this line" rules is unreliable for empty-payload turns, so
 * we short-circuit at the slackbot to guarantee the persona identifier
 * appears and to save a sandbox round-trip.
 */
const BARE_FLAG_GREETINGS: Record<string, string> = {
  invest: "Spock — Paradigm's investment agent. What are we looking at?",
};

/** Return the canned greeting for a bare `--<persona>` mention with no other content. */
export function bareFlagGreeting(
  selector: string | undefined,
  cleanedText: string,
  attachmentCount: number,
): string | undefined {
  if (!selector) return undefined;
  if (attachmentCount > 0) return undefined;
  if (stripMentions(cleanedText).length > 0) return undefined;
  return BARE_FLAG_GREETINGS[selector];
}

/** Extract text from a message, preferring the formatted AST (preserves links) over plain text. */
function richTextFromMessage(msg: { text: string; formatted?: Root }): string {
  if (msg.formatted) {
    return stringifyMarkdown(msg.formatted).trim();
  }
  return (msg.text || "").trim();
}

function stableSlackMessageId(msg: { id?: string; raw?: SlackRawMessage }): string | undefined {
  const stableId = msg.id || msg.raw?.ts;
  return stableId ? `slack:${stableId}` : undefined;
}

function slackTeamId(msg: { raw?: SlackRawMessage }): string | undefined {
  const teamId = msg.raw?.team_id ?? msg.raw?.team;
  return typeof teamId === "string" && teamId.trim() ? teamId : undefined;
}

function promptSelectorToSpawnOptions(promptSelector?: string): { harness?: string; personaId?: string } {
  if (!promptSelector) return {};
  return EXECUTION_HARNESSES.has(promptSelector)
    ? { harness: promptSelector }
    : { personaId: promptSelector };
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? value as Record<string, unknown> : {};
}

function slackAdapterThreadId(threadKey: string): string {
  return threadKey.startsWith("slack:") ? threadKey : `slack:${threadKey}`;
}

function slackDeliveryThreadId(threadKey: string, delivery: Record<string, unknown>): string {
  const channel = typeof delivery.channel === "string" ? delivery.channel.trim() : "";
  const threadTs = typeof delivery.thread_ts === "string" ? delivery.thread_ts.trim() : "";
  if (channel && threadTs) {
    return `slack:${channel}:${threadTs}`;
  }
  return slackAdapterThreadId(threadKey);
}

function slackLink(url: string, label: string): string {
  return `<${url}|${label}>`;
}

type SlackBlocks = Extract<StreamChunk, { type: "blocks" }>["blocks"];

function splitSlackBlocks(blocks: SlackBlocks): SlackBlocks[] {
  const chunks: SlackBlocks[] = [];
  for (let i = 0; i < blocks.length; i += 50) {
    chunks.push(blocks.slice(i, i + 50));
  }
  return chunks;
}

// ── Types ─────────────────────────────────────────────────────────────────

export interface BotThread {
  id: string;
  subscribe(): Promise<void>;
  startTyping(status?: string): Promise<void>;
  stopTyping?(): Promise<void>;
  post(content: AsyncGenerator<StreamChunk> | { markdown: string }, options?: { taskDisplayMode?: "timeline" | "plan" }): Promise<{ id: string; edit(content: { markdown: string }): Promise<void> }>;
}

export interface BotMessage {
  id?: string;
  text: string;
  formatted?: Root;
  isMention?: boolean;
  raw?: SlackRawMessage;
  author: { isMe: boolean; isBot: boolean; userId?: string };
  attachments?: BotAttachment[];
}

export interface BotAttachment {
  url?: string;
  name?: string;
  mimeType?: string;
  fetchData?: () => Promise<Buffer>;
}

export interface SlackAdapter {
  fetchMessage(threadId: string, ts: string): Promise<{ attachments?: BotAttachment[] } | null>;
  fetchMessages(threadId: string, options?: { direction?: "forward" | "backward"; limit?: number }): Promise<{ messages: Array<{ id?: string; text: string; formatted?: Root; raw?: SlackRawMessage; author: { isMe: boolean; isBot: boolean; userId: string }; attachments?: BotAttachment[] }> }>;
  setAssistantTitle(channel: string, threadTs: string, title: string): Promise<void>;
  postMessage(threadId: string, message: { markdown: string }): Promise<{ id: string }>;
  getInstallation?(teamId: string): Promise<{ botToken: string } | null>;
  withBotToken?<T>(token: string, fn: () => Promise<T> | T): Promise<T>;
}

// ── Bot ───────────────────────────────────────────────────────────────────
//
// Mental model:
//   - First mention  → spawn assignment + buffer message + execute + stream durable events
//   - Non-mention in subscribed thread → buffer message (context only)
//   - Mention in subscribed thread → buffer + execute against current assignment_generation
//

export class SlackBot {
  /** Ephemeral UX coordination only — durable correctness lives in Postgres. */
  private inFlightExecutions = new Map<string, InFlightExecution>();

  private finalDeliveryLoop: Promise<void> | null = null;

  private readonly deliveryConsumerId = `slackbot:${process.env.HOSTNAME || "local"}`;

  constructor(
    readonly client: CentaurClient,
    private viewerUrl = "",
    private slack?: SlackAdapter,
  ) {}

  static createFromEnv(slack?: SlackAdapter): SlackBot {
    return new SlackBot(
      new CentaurClient({
        apiUrl: process.env.CENTAUR_API_URL || "http://api:8000",
        apiKey: process.env.SLACKBOT_API_KEY || process.env.API_SECRET_KEY || "",
        logger: log,
      }),
      process.env.THREAD_VIEWER_URL || "",
      slack,
    );
  }

  startFinalDeliveryWorker() {
    if (!this.slack || this.finalDeliveryLoop) return;
    this.finalDeliveryLoop = this.runFinalDeliveryLoop();
  }

  private async runFinalDeliveryLoop(): Promise<void> {
    while (true) {
      try {
        const processed = await this.drainFinalDeliveriesOnce();
        await sleep(processed > 0 ? 0 : FINAL_DELIVERY_IDLE_MS);
      } catch (err) {
        log.error("final_delivery_loop_failed", {
          error: err instanceof Error ? err.message : String(err),
        });
        await sleep(FINAL_DELIVERY_ERROR_MS);
      }
    }
  }

  private async drainFinalDeliveriesOnce(): Promise<number> {
    if (!this.slack) return 0;
    const { deliveries } = await this.client.claimFinalDeliveries({
      consumerId: this.deliveryConsumerId,
      limit: FINAL_DELIVERY_BATCH_SIZE,
      leaseSeconds: FINAL_DELIVERY_LEASE_SECONDS,
      platform: "slack",
    });
    for (const delivery of deliveries as FinalDeliveryRecord[]) {
      log.info("final_delivery_claimed", {
        thread_key: delivery.thread_key,
        execution_id: delivery.execution_id,
        attempt_count: delivery.attempt_count,
      });
      await this.processFinalDelivery(delivery);
    }
    return deliveries.length;
  }

  // ── Handlers ────────────────────────────────────────────────────────────

  async onNewMention(thread: BotThread, msg: BotMessage) {
    if (msg.author.isMe || msg.author.isBot) return;
    const threadKey = normalizeThreadKey(thread.id);
    log.info("mention_received", { thread_key: threadKey, user_id: msg.author.userId, is_new_thread: true });
    await thread.subscribe();
    thread.startTyping().catch(() => {});

    const richText = richTextFromMessage(msg);
    const { selector: promptSelector, cleaned } = extractFlagSelector(richText);
    // If flag-stripping leaves only the bot mention or nothing, the agent gets
    // an empty text — never `--invest`. The bare-flag short-circuit below
    // catches the empty case before we reach the agent.
    const agentText = cleaned;

    // Bare-flag short-circuit: respond with the canned persona greeting
    // without spinning up an LLM. Guarantees the persona identifier appears
    // and saves a ~6s round-trip + token cost.
    const attachments = await this.resolveAttachments(thread.id, msg);
    const greeting = bareFlagGreeting(promptSelector, cleaned, attachments.length);
    if (greeting) {
      log.info("bare_flag_greeting", { thread_key: threadKey, selector: promptSelector });
      await thread.post({ markdown: greeting });
      return;
    }

    // Buffer prior thread messages as context before the mentioning message
    await this.backfillThreadHistory(thread.id, promptSelector);

    const parts = await this.toParts(agentText, attachments);
    await this.bufferAndExecuteSafely(thread, agentText, parts, {
      messageId: stableSlackMessageId(msg),
      userId: msg.author.userId,
      teamId: slackTeamId(msg),
    }, promptSelector);
  }

  async onSubscribedMessage(thread: BotThread, msg: BotMessage) {
    if (msg.author.isMe || msg.author.isBot) return;

    const attachments = msg.isMention ? await this.resolveAttachments(thread.id, msg) : (msg.attachments || []);
    const text = richTextFromMessage(msg);
    if (!text && !attachments.length) return;

    if (msg.isMention) {
      const { selector: promptSelector, cleaned } = extractFlagSelector(text);
      const greeting = bareFlagGreeting(promptSelector, cleaned, attachments.length);
      if (greeting) {
        log.info("bare_flag_greeting", { thread_key: normalizeThreadKey(thread.id), selector: promptSelector, is_new_thread: false });
        await thread.post({ markdown: greeting });
        return;
      }
      const agentText = cleaned;
      const parts = await this.toParts(agentText || "Shared attachment in thread.", attachments);
      log.info("mention_received", { thread_key: normalizeThreadKey(thread.id), user_id: msg.author.userId, is_new_thread: false });
      thread.startTyping().catch(() => {});
      await this.bufferAndExecuteSafely(thread, agentText, parts, {
        messageId: stableSlackMessageId(msg),
        userId: msg.author.userId,
        teamId: slackTeamId(msg),
      }, promptSelector);
      return;
    }

    const parts = await this.toParts(text || "Shared attachment in thread.", attachments);

    const threadKey = normalizeThreadKey(thread.id);
    const state = await this.ensureAssignment(threadKey);

    try {
      await this.client.message({
        threadKey,
        assignmentGeneration: state.assignmentGeneration,
        messageId: stableSlackMessageId(msg),
        parts,
        userId: msg.author.userId,
      });
      log.info("message_buffered", {
        thread_key: threadKey,
        message_id: stableSlackMessageId(msg),
        assignment_generation: state.assignmentGeneration,
        is_mention: false,
      });
    } catch (err) {
      log.warn("message_buffer_failed", { thread_key: normalizeThreadKey(thread.id), error: err instanceof Error ? err.message : String(err) });
    }
  }

  // ── Core ────────────────────────────────────────────────────────────────

  private async bufferAndExecuteSafely(
    thread: BotThread,
    text: string,
    parts: InputContentBlock[],
    delivery: DeliveryContext,
    promptSelectorOverride?: string,
  ) {
    try {
      await this.bufferAndExecute(thread, text, parts, delivery, promptSelectorOverride);
    } catch (err) {
      const error = err instanceof Error ? err.message : String(err);
      log.error("execute_start_failed", {
        thread_key: normalizeThreadKey(thread.id),
        error,
      });
      await thread.stopTyping?.();
      await thread.post({ markdown: "Agent request failed before execution started. Please retry." });
    }
  }

  private async bufferAndExecute(
    thread: BotThread,
    text: string,
    parts: InputContentBlock[],
    delivery: DeliveryContext,
    promptSelectorOverride?: string,
  ) {
    const threadKey = normalizeThreadKey(thread.id);
    await this.cancelInflightExecution(threadKey);
    const promptSelector = promptSelectorOverride ?? parsePromptSelectorFlag(text);
    if (promptSelector) {
      await this.releaseForPromptSwitch(threadKey, delivery.messageId);
    }
    const { channel, threadTs } = splitThreadKey(thread.id);
    const accepted = await this.client.startWorkflowRun({
      workflowName: "slack_thread_turn",
      triggerKey: delivery.messageId ? `slack-thread-turn:${threadKey}:${delivery.messageId}` : undefined,
      eagerStart: true,
      input: {
        thread_key: threadKey,
        parts,
        user_id: delivery.userId,
        message_id: delivery.messageId,
        prompt_selector: promptSelector,
        delivery: {
          channel,
          thread_ts: threadTs,
          platform: "slack",
          recipient_user_id: delivery.userId,
          recipient_team_id: delivery.teamId,
        },
      },
    });
    if (!accepted.execution_id) {
      const errorText = typeof accepted.error_text === "string"
        ? accepted.error_text.trim()
        : "";
      throw new Error(errorText || "workflow did not enqueue an execution");
    }
    await this.execute(thread, threadKey, {
      executionId: accepted.execution_id,
      userId: delivery.userId,
      teamId: delivery.teamId,
    });
  }

  private async releaseForPromptSwitch(threadKey: string, messageId?: string): Promise<void> {
    try {
      const releaseId = messageId
        ? `prompt-switch:${messageId}`
        : `prompt-switch:${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
      await this.client.releaseThread(threadKey, {
        releaseId,
        cancelInflight: true,
      });
      log.info("prompt_switch_released_assignment", {
        thread_key: threadKey,
        release_id: releaseId,
      });
    } catch (err) {
      log.warn("prompt_switch_release_failed", {
        thread_key: threadKey,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private async execute(thread: BotThread, threadKey: string, opts: { assignmentGeneration?: number; executionId?: string; userId?: string; teamId?: string }) {
    const ac = new AbortController();

    const tracker = new ProgressTracker();
    const t0 = Date.now();
    let executionId = typeof opts.executionId === "string" ? opts.executionId : "";
    try {
      if (!executionId) {
        if (typeof opts.assignmentGeneration !== "number") {
          throw new Error("missing assignmentGeneration for direct execute path");
        }
        const { channel, threadTs } = splitThreadKey(thread.id);
        const executeId = `exec-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`;
        const accepted = await this.client.execute({
          threadKey,
          assignmentGeneration: opts.assignmentGeneration,
          executeId,
          platform: "slack",
          userId: opts.userId,
          delivery: {
            channel,
            thread_ts: threadTs,
            platform: "slack",
            recipient_user_id: opts.userId,
            recipient_team_id: opts.teamId,
          },
        });
        executionId = accepted.execution_id;
      }
      this.inFlightExecutions.set(threadKey, {
        executionId,
        abortController: ac,
      });
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err);
      log.error("execute_enqueue_failed", { thread_key: threadKey, error: errMsg });
      try {
        await thread.post({ markdown: `Agent request failed before execution started: ${errMsg}` });
      } catch {
        // best-effort
      }
      return;
    }

    log.info("execute_start", { thread_key: threadKey, user_id: opts.userId, execution_id: executionId });

    let deliveredToSlack = false;
    try {
      try {
        const stream = this.streamExecution(threadKey, executionId, tracker, t0, ac.signal);
        const iter = stream[Symbol.asyncIterator]();
        let firstChunk: StreamChunk | undefined;

        while (!ac.signal.aborted) {
          const next = await iter.next();
          if (next.done) break;
          firstChunk = next.value;
          break;
        }

        if (firstChunk) {
          await thread.post(
            (async function* () {
              yield firstChunk;
              while (true) {
                const next = await iter.next();
                if (next.done) return;
                yield next.value;
              }
            })(),
            { taskDisplayMode: "plan" },
          );
          deliveredToSlack = true;
        }
      } catch (err) {
        const errMsg = err instanceof Error ? err.message : String(err);

        // Slack killed the streaming state before we called stop() (long-running turn),
        // or the accumulated streamed text exceeded Slack's message length limit.
        // Fall back to posting a plain message with whatever result we accumulated,
        // or poll the API for the final result if we don't have one yet.
        if (
          errMsg.includes("message_not_in_streaming_state")
          || errMsg.includes("msg_too_long")
          || errMsg.includes("streaming_mode_mismatch")
          || errMsg.includes("cannot_provide_both_markdown_text_and_chunks")
        ) {
          log.warn("slack_stream_fallback", { thread_key: threadKey, error: errMsg, execution_id: executionId });
          let fallback = convertDashboardBlocks((tracker.resultText || tracker.lastAssistantText).trim());

          if (!fallback) {
            fallback = convertDashboardBlocks(await this.pollForResult(executionId));
          }

          if (fallback) {
            for (const chunk of splitSlackMessage(fallback)) {
              await thread.post({ markdown: chunk });
            }
            deliveredToSlack = true;
          } else if (this.viewerUrl) {
            const viewerLink = `${this.viewerUrl}/${encodeURIComponent(normalizeThreadKey(threadKey))}`;
            await thread.post({ markdown: `Agent completed. [View full output](${viewerLink})` });
            deliveredToSlack = true;
          }
          if (deliveredToSlack) {
            await this.ackFinalDelivery(executionId, threadKey, { requireLease: false });
          }
          return;
        }

        log.error("execute_error", { thread_key: threadKey, error: errMsg, execution_id: executionId });
        try {
          await thread.post({ markdown: `Agent request failed: ${errMsg}` });
        } catch (postErr) {
          log.error("error_post_failed", { thread_key: threadKey, error: postErr instanceof Error ? postErr.message : String(postErr) });
        }
        return;
      }

      // Post any overflow chunks that didn't fit in the streaming message
      for (const chunk of tracker.overflowChunks) {
        try {
          await thread.post({ markdown: chunk });
        } catch (err) {
          log.warn("overflow_post_failed", { thread_key: threadKey, error: err instanceof Error ? err.message : String(err) });
        }
      }

      const finalText = (tracker.resultText || tracker.lastAssistantText).trim();
      log.info("execute_complete", { thread_key: threadKey, duration_s: Math.round((Date.now() - t0) / 100) / 10, result_length: finalText.length, overflow_chunks: tracker.overflowChunks.length });

      if (deliveredToSlack) {
        await this.ackFinalDelivery(executionId, threadKey, { requireLease: false });
      }

      await this.setAssistantTitle(threadKey, {}, finalText);
    } finally {
      const durationMs = Date.now() - t0;
      log.info("execute_completed", {
        thread_key: threadKey,
        execution_id: executionId,
        duration_ms: durationMs,
        delivered_to_slack: deliveredToSlack,
      });
      const current = this.inFlightExecutions.get(threadKey);
      if (current?.executionId === executionId) {
        this.inFlightExecutions.delete(threadKey);
      }
    }
  }

  private async pollForResult(executionId: string): Promise<string> {
    const deadline = Date.now() + STREAM_EXPIRED_POLL_MAX_MS;
    while (Date.now() < deadline) {
      try {
        const data = await this.client.getExecution(executionId);
        const status = String(data.status || "");
        const text = renderTerminalResultCopy({
          status,
          terminalReason: data.terminal_reason,
          resultText: data.result_text,
          errorText: data.error_text,
        });
        if (text) {
          return text;
        }
      } catch {
        // best-effort — keep polling
      }
      await new Promise((r) => setTimeout(r, STREAM_EXPIRED_POLL_INTERVAL_MS));
    }
    return "";
  }

  private async hydrateStoredTerminalResult(
    executionId: string,
    tracker: ProgressTracker,
  ): Promise<boolean> {
    try {
      const data = await this.client.getExecution(executionId);
      const status = String(data.status || "");
      if (!["completed", "failed_permanent", "cancelled"].includes(status)) {
        return false;
      }

      const result = typeof data.result_text === "string" ? data.result_text.trim() : "";
      const error = typeof data.error_text === "string" ? data.error_text.trim() : "";
      const terminalReason = typeof data.terminal_reason === "string"
        ? data.terminal_reason.trim()
        : "";
      tracker.resultText = renderTerminalResultCopy({
        status,
        terminalReason,
        resultText: result,
        errorText: error,
      });
      return true;
    } catch {
      return false;
    }
  }

  private async *streamExecution(
    threadKey: string,
    executionId: string,
    tracker: ProgressTracker,
    t0: number,
    signal: AbortSignal,
  ): AsyncGenerator<StreamChunk> {
    yield* this.consumeExecutionEvents(threadKey, executionId, tracker, signal);

    // If aborted, don't emit any final output — the new stream owns it
    if (signal.aborted) return;

    // Complete all in-progress steps and set plan title to "Completed"
    yield* tracker.finalize();

    // Emit the final response after a context block with run metadata.
    // If the text exceeds Slack's 4k char limit, yield only the first chunk here
    // and stash overflow for the caller to post as separate messages.
    // Convert ```dashboard blocks to markdown tables so they render as Slack Block Kit.
    const finalText = convertDashboardBlocks((tracker.resultText || tracker.lastAssistantText).trim());
    if (finalText) {
      const dur = (Date.now() - t0) / 1000;
      const durStr = dur < 10 ? `${dur.toFixed(1)}s` : `${Math.round(dur)}s`;
      const harness = tracker.agentThreadId
        ? slackLink(`https://ampcode.com/threads/${tracker.agentThreadId}`, "agent")
        : "agent";
      const suffix = this.viewerUrl ? `\n\n[Thread Viewer](${this.viewerUrl}/${encodeURIComponent(threadKey)})` : "";
      const fullMd = `${finalText}${suffix}`;
      yield {
        type: "blocks",
        blocks: [{
          type: "context",
          elements: [{ type: "mrkdwn", text: [process.env.APP_NAME || "Centaur", harness, durStr].join(" · ") }],
        }],
      };

      const rendered = renderMarkdownForSlack(fullMd);
      if (rendered.blocks) {
        for (const blocks of splitSlackBlocks(rendered.blocks)) {
          yield { type: "blocks", blocks };
        }
        tracker.overflowChunks = [];
      } else {
        const chunks = splitSlackMessage(fullMd);
        yield { type: "markdown_text", text: chunks[0] };
        tracker.overflowChunks = chunks.slice(1);
      }
    } else {
      yield { type: "markdown_text", text: "Agent completed with no output." };
    }
  }

  /**
   * Consume durable execution events until terminal state, reconnecting from
   * the last durable cursor on transient stream failures.
   */
  private async *consumeExecutionEvents(
    threadKey: string,
    executionId: string,
    tracker: ProgressTracker,
    signal: AbortSignal,
  ): AsyncGenerator<StreamChunk> {
    let retriesLeft = RECONNECT_MAX_RETRIES;
    let afterEventId = 0;
    log.info("stream_started", { thread_key: threadKey, execution_id: executionId });

    while (true) {
      const stream = this.client.streamEvents({
        threadKey,
        executionId,
        afterEventId,
        signal,
      });
      const iter = stream[Symbol.asyncIterator]();
      let pending: Promise<IteratorResult<{ eventId: number; eventKind: string; data: Record<string, unknown> }, void>> | null = null;
      let streamBroke = false;
      let terminal = false;

      try {
        while (true) {
          if (signal.aborted) {
            log.info("stream_aborted", { thread_key: threadKey });
            return;
          }

          if (!pending) pending = iter.next();

          const raced = await Promise.race([
            pending.then((r) => ({ kind: "value" as const, result: r })),
            new Promise<{ kind: "keepalive" }>((r) => setTimeout(() => r({ kind: "keepalive" }), KEEPALIVE_MS)),
            new Promise<{ kind: "aborted" }>((r) => {
              if (signal.aborted) { r({ kind: "aborted" }); return; }
              signal.addEventListener("abort", () => r({ kind: "aborted" }), { once: true });
            }),
          ]);

          if (raced.kind === "aborted") {
            log.info("stream_aborted", { thread_key: threadKey });
            return;
          }

          if (raced.kind === "keepalive") {
            // Invisible markdown keepalive prevents Slack from expiring the stream
            // on long-running turns even when there is no user-visible text yet.
            yield { type: "markdown_text", text: STREAM_BOOTSTRAP_TEXT };
            yield { type: "plan_update", title: "Still working…" };
            continue;
          }

          pending = null;
          if (raced.result.done) {
            streamBroke = true;
            break;
          }
          const streamEvent = raced.result.value;
          afterEventId = Math.max(afterEventId, streamEvent.eventId || 0);
          const payload = streamEvent.data;
          const eventType = typeof payload.type === "string" ? payload.type : "";

          if (eventType === "turn.done") {
            const result = String(payload.result || "").trim();
            const errorText = String(payload.error || "").trim();
            const rendered = renderTerminalResultCopy({
              resultText: result,
              errorText,
              isError: payload.is_error,
            });
            if (rendered) tracker.resultText = rendered;
            terminal = true;
            break;
          }

          if (eventType === "execution.state") {
            const status = String(payload.status || "");
            if (["completed", "failed_permanent", "cancelled"].includes(status)) {
              const rendered = renderTerminalResultCopy({
                status,
                terminalReason: payload.terminal_reason,
                resultText: payload.result_text,
                errorText: payload.error_text,
              });
              if (rendered) tracker.resultText = rendered;
              terminal = true;
              break;
            }
            continue;
          }

          if (eventType.startsWith("final_delivery.")) continue;

          yield* tracker.update(payload as unknown as CanonicalEvent);
        }
      } catch {
        streamBroke = true;
      }

      if (terminal) {
        log.info("stream_completed", { thread_key: threadKey, execution_id: executionId, last_event_id: afterEventId });
        return;
      }
      if (await this.hydrateStoredTerminalResult(executionId, tracker)) {
        log.info("stream_completed", { thread_key: threadKey, execution_id: executionId, last_event_id: afterEventId });
        return;
      }
      if (!streamBroke) {
        log.info("stream_completed", { thread_key: threadKey, execution_id: executionId, last_event_id: afterEventId });
        return;
      }

      if (retriesLeft <= 0 || signal.aborted) {
        log.warn("wire_reconnect_exhausted", { thread_key: threadKey, execution_id: executionId });
        log.info("stream_completed", { thread_key: threadKey, execution_id: executionId, last_event_id: afterEventId });
        return;
      }

      const delay = RECONNECT_BASE_DELAY_MS * (RECONNECT_MAX_RETRIES - retriesLeft + 1);
      log.info("wire_reconnecting", { thread_key: threadKey, retries_left: retriesLeft, delay_ms: delay });
      yield { type: "plan_update", title: "Reconnecting…" };
      await new Promise((r) => setTimeout(r, delay));
      retriesLeft--;

      if (signal.aborted) return;
    }
  }

  private async ackFinalDelivery(
    executionId: string,
    threadKey: string,
    opts?: { requireLease?: boolean },
  ): Promise<void> {
    try {
      await this.client.markFinalDelivered(
        executionId,
        opts?.requireLease === false ? undefined : this.deliveryConsumerId,
      );
    } catch (err) {
      log.warn("final_delivery_ack_failed", {
        thread_key: threadKey,
        execution_id: executionId,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private async failFinalDelivery(
    executionId: string,
    threadKey: string,
    error: string,
    opts?: { nonRetryable?: boolean; errorClass?: string },
  ): Promise<void> {
    try {
      await this.client.markFinalFailed(executionId, error, {
        consumerId: this.deliveryConsumerId,
        nonRetryable: opts?.nonRetryable,
        errorClass: opts?.errorClass,
      });
    } catch (err) {
      log.warn("final_delivery_fail_mark_failed", {
        thread_key: threadKey,
        execution_id: executionId,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private async cancelInflightExecution(threadKey: string): Promise<void> {
    const current = this.inFlightExecutions.get(threadKey);
    if (!current) return;

    this.inFlightExecutions.delete(threadKey);
    current.abortController.abort();
    log.info("cancelling_previous_execution", {
      thread_key: threadKey,
      execution_id: current.executionId,
    });

    try {
      await this.client.cancelExecution(current.executionId);
    } catch (err) {
      log.warn("cancel_previous_execution_failed", {
        thread_key: threadKey,
        execution_id: current.executionId,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  private async ensureAssignment(
    threadKey: string,
    promptSelector?: string,
  ): Promise<{ assignmentGeneration: number }> {
    const spawn = await this.client.spawn({
      threadKey,
      spawnId: `spawn-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`,
      ...promptSelectorToSpawnOptions(promptSelector),
    });
    log.info("assignment_ready", {
      thread_key: threadKey,
      assignment_generation: Number(spawn.assignment_generation || 0),
      runtime_id: spawn.runtime_id,
      prompt_ref: spawn.prompt_ref,
      prompt_sha: spawn.effective_agents_md_sha256,
      prompt_selector: promptSelector,
    });
    return {
      assignmentGeneration: Number(spawn.assignment_generation || 0),
    };
  }

  // ── Helpers ─────────────────────────────────────────────────────────────

  private async processFinalDelivery(record: FinalDeliveryRecord): Promise<void> {
    if (!this.slack) return;

    const executionId = typeof record.execution_id === "string" ? record.execution_id : "";
    const threadKey = typeof record.thread_key === "string" ? record.thread_key : "";
    const delivery = asRecord(record.delivery);
    const finalPayload = asRecord(record.final_payload);

    if (!executionId || !threadKey) return;
    log.info("final_delivery_started", { thread_key: threadKey, execution_id: executionId });

    if (this.isExecutionStreaming(executionId)) {
      log.info("final_delivery_deferred_live_stream", {
        execution_id: executionId,
        thread_key: threadKey,
      });
      try {
        await this.client.renewFinalDeliveryLease(executionId, {
          consumerId: this.deliveryConsumerId,
          leaseSeconds: FINAL_DELIVERY_LEASE_SECONDS,
        });
      } catch (err) {
        log.warn("final_delivery_defer_lease_refresh_failed", {
          execution_id: executionId,
          thread_key: threadKey,
          error: err instanceof Error ? err.message : String(err),
        });
      }
      // Keep the claimed lease intact so the live stream can ack the same
      // outbox row on completion instead of racing a retry into a duplicate post.
      return;
    }

    if (await this.shouldSuppressFinalDelivery(threadKey, executionId, finalPayload)) {
      log.info("final_delivery_suppressed", {
        execution_id: executionId,
        thread_key: threadKey,
        status: finalPayload.status,
        terminal_reason: finalPayload.terminal_reason,
      });
      await this.ackFinalDelivery(executionId, threadKey);
      return;
    }

    const markdown = this.renderFinalDeliveryMarkdown(threadKey, finalPayload);
    try {
      await this.postSlackMarkdown(threadKey, delivery, markdown);
      await this.ackFinalDelivery(executionId, threadKey);
      await this.setAssistantTitle(threadKey, delivery, markdown);
      log.info("final_delivery_completed", { thread_key: threadKey, execution_id: executionId });
    } catch (err) {
      let error = err instanceof Error ? err.message : String(err);
      if (isSlackInvalidBlocksError(error)) {
        const fallbackMarkdown = flattenMarkdownTables(markdown);
        if (fallbackMarkdown !== markdown) {
          try {
            log.warn("final_delivery_retry_plaintext", {
              execution_id: executionId,
              thread_key: threadKey,
            });
            await this.postSlackMarkdown(threadKey, delivery, fallbackMarkdown);
            await this.ackFinalDelivery(executionId, threadKey);
            await this.setAssistantTitle(threadKey, delivery, fallbackMarkdown);
            log.info("final_delivery_completed", {
              thread_key: threadKey,
              execution_id: executionId,
              downgraded_tables: true,
            });
            return;
          } catch (fallbackErr) {
            error = fallbackErr instanceof Error ? fallbackErr.message : String(fallbackErr);
          }
        }
      }
      const classified = classifySlackError(error);
      log.warn("final_delivery_post_failed", {
        execution_id: executionId,
        thread_key: threadKey,
        error: classified.message,
        error_class: classified.errorClass,
        error_code: classified.code,
        status: classified.status,
        retryable: classified.retryable,
      });
      await this.failFinalDelivery(executionId, threadKey, classified.message, {
        nonRetryable: !classified.retryable,
        errorClass: classified.errorClass,
      });
    }
  }

  private isExecutionStreaming(executionId: string): boolean {
    for (const current of this.inFlightExecutions.values()) {
      if (current.executionId === executionId && !current.abortController.signal.aborted) {
        return true;
      }
    }
    return false;
  }

  private async shouldSuppressFinalDelivery(
    threadKey: string,
    executionId: string,
    finalPayload: Record<string, unknown>,
  ): Promise<boolean> {
    const status = typeof finalPayload.status === "string" ? finalPayload.status : "";
    const terminalReason = typeof finalPayload.terminal_reason === "string"
      ? finalPayload.terminal_reason
      : "";
    const resultText = normalizedTerminalString(finalPayload.result_text);
    const errorText = normalizedTerminalString(finalPayload.error_text);
    if (!isCancellationTerminalState(status, terminalReason, resultText, errorText)) {
      return false;
    }

    const current = this.inFlightExecutions.get(threadKey);
    if (current && current.executionId !== executionId && !current.abortController.signal.aborted) {
      return true;
    }

    try {
      const { executions } = await this.client.listExecutions(threadKey, 2);
      const latestExecutionId = Array.isArray(executions) && typeof executions[0]?.execution_id === "string"
        ? executions[0].execution_id
        : "";
      return Boolean(latestExecutionId && latestExecutionId !== executionId);
    } catch (err) {
      log.warn("final_delivery_suppress_lookup_failed", {
        thread_key: threadKey,
        execution_id: executionId,
        error: err instanceof Error ? err.message : String(err),
      });
      return false;
    }
  }

  private renderFinalDeliveryMarkdown(threadKey: string, finalPayload: Record<string, unknown>): string {
    const status = typeof finalPayload.status === "string" ? finalPayload.status : "";
    const terminalReason = typeof finalPayload.terminal_reason === "string"
      ? finalPayload.terminal_reason
      : "";
    const resultText = typeof finalPayload.result_text === "string" ? finalPayload.result_text.trim() : "";
    const errorText = typeof finalPayload.error_text === "string" ? finalPayload.error_text.trim() : "";
    const rendered = convertDashboardBlocks(renderTerminalResultCopy({
      status,
      terminalReason,
      resultText,
      errorText,
    }));
    const viewerSuffix = this.viewerUrl
      ? `\n\n[Thread Viewer](${this.viewerUrl}/${encodeURIComponent(threadKey)})`
      : "";

    if (rendered) {
      return `${rendered}${viewerSuffix}`;
    }

    if (this.viewerUrl) {
      return `Agent completed. [View full output](${this.viewerUrl}/${encodeURIComponent(threadKey)})`;
    }

    return "Agent completed with no output.";
  }

  private async postSlackMarkdown(
    threadKey: string,
    delivery: Record<string, unknown>,
    markdown: string,
  ): Promise<void> {
    const targetThreadId = slackDeliveryThreadId(threadKey, delivery);
    await this.withSlackDeliveryContext(delivery, async () => {
      for (const chunk of splitSlackMessage(markdown)) {
        await this.slack!.postMessage(targetThreadId, { markdown: chunk });
      }
    });
  }

  private async withSlackDeliveryContext<T>(
    delivery: Record<string, unknown>,
    fn: () => Promise<T>,
  ): Promise<T> {
    if (!this.slack?.withBotToken || !this.slack.getInstallation) {
      return fn();
    }

    const teamId = typeof delivery.recipient_team_id === "string"
      ? delivery.recipient_team_id.trim()
      : "";
    if (!teamId) return fn();

    const installation = await this.slack.getInstallation(teamId);
    if (!installation?.botToken) {
      log.warn("final_delivery_missing_installation", { team_id: teamId });
      return fn();
    }

    return this.slack.withBotToken(installation.botToken, fn);
  }

  private async setAssistantTitle(
    threadKey: string,
    delivery: Record<string, unknown>,
    title: string,
  ): Promise<void> {
    if (!this.slack || !title.trim()) return;

    try {
      const { channel, threadTs } = splitThreadKey(slackDeliveryThreadId(threadKey, delivery));
      await this.slack.setAssistantTitle(channel, threadTs, title.slice(0, 60));
    } catch (err) {
      log.warn("set_title_failed", {
        thread_key: threadKey,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  }

  /** Fetch prior thread messages and buffer them to the API so the agent has full context. */
  private async backfillThreadHistory(threadId: string, promptSelector?: string) {
    if (!this.slack) return;
    const threadKey = normalizeThreadKey(threadId);
    const state = await this.ensureAssignment(threadKey, promptSelector);
    try {
      const { messages } = await this.slack.fetchMessages(threadId, { direction: "forward", limit: 50 });
      // Skip the last message (the mention itself — it gets buffered by the caller)
      const prior = messages.filter((m) => !m.author.isMe && !m.author.isBot);
      if (!prior.length) return;
      // Drop the last non-bot message since it's the mentioning message buffered by bufferAndExecute
      const history = prior.slice(0, -1);
      for (const m of history) {
        const text = richTextFromMessage(m);
        const attachments = m.attachments || [];
        if (!text && !attachments.length) continue;
        const parts = await this.toParts(text || "Shared attachment in thread.", attachments);
        await this.client.message({
          threadKey,
          assignmentGeneration: state.assignmentGeneration,
          messageId: stableSlackMessageId(m),
          parts,
          userId: m.author.userId,
        });
      }
      if (history.length) {
        log.info("thread_history_backfilled", { thread_key: threadKey, count: history.length });
      }
    } catch (err) {
      log.warn("thread_history_backfill_failed", { thread_key: threadKey, error: err instanceof Error ? err.message : String(err) });
    }
  }

  async resolveAttachments(threadId: string, msg: BotMessage): Promise<BotAttachment[]> {
    if (msg.attachments?.length) return [...msg.attachments];
    const ts = msg.id || msg.raw?.ts || "";
    if (!ts || !this.slack) return [];
    try {
      const refetched = await this.slack.fetchMessage(threadId, ts);
      if (refetched?.attachments?.length) {
        log.info("mention_files_refetched", { thread_key: normalizeThreadKey(threadId), count: refetched.attachments.length });
        return [...refetched.attachments];
      }
    } catch (err) {
      log.warn("mention_files_refetch_failed", { thread_key: normalizeThreadKey(threadId), error: err instanceof Error ? err.message : String(err) });
    }
    return [];
  }

  async toParts(text: string, attachments: BotAttachment[]): Promise<InputContentBlock[]> {
    const parts: InputContentBlock[] = [{ type: "text", text }];
    for (const att of attachments) {
      if (!att.fetchData || !att.mimeType) continue;
      try {
        const data = await att.fetchData();
        const b64 = data.toString("base64");
        const source = { type: "base64" as const, media_type: att.mimeType, data: b64 };
        parts.push(att.mimeType.startsWith("image/") ? { type: "image", source } : { type: "document", source });
      } catch (err) {
        log.warn("attachment_fetch_failed", { name: att.name || "unknown", error: err instanceof Error ? err.message : String(err) });
      }
    }
    return parts;
  }
}
