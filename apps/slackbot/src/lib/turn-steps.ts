/**
 * Convert Postgres turns directly into Step[] for rendering in ActivityFeed.
 *
 * This avoids the SSE round-trip for historical/idle threads — we interpret
 * turn.events client-side using the same logic the backend uses when building
 * UI stream chunks.
 */
import type { LucideIcon } from "lucide-react";
import {
  categorizeToolCall,
  summarizeGroup,
  type ContextMessageItem,
  type Step,
  type ToolCall,
} from "@/lib/describe";
import type { Turn } from "@/lib/types";

function asString(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function asRecord(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" ? (value as Record<string, unknown>) : {};
}

function outputToText(output: unknown): string | undefined {
  if (output === undefined || output === null) return undefined;
  if (typeof output === "string") return output;
  try {
    return JSON.stringify(output, null, 2);
  } catch {
    return String(output);
  }
}

/** Parse a "[phase]" label from the beginning of a user message. */
function parsePhaseLabel(message: string): string | null {
  if (!message.startsWith("[")) return null;
  const closing = message.indexOf("]");
  if (closing <= 1) return null;
  return message.slice(1, closing).trim().toLowerCase() || null;
}

/** Strip internal context headers from user messages for display. */
function displayUserMessage(text: string): string {
  let cleaned = text.trim();
  if (!cleaned) return "";
  const contextHeader =
    "Additional Slack thread context since the last AI instruction (ambient discussion from humans):";
  const contextIdx = cleaned.indexOf(contextHeader);
  if (contextIdx >= 0) {
    cleaned = cleaned.slice(0, contextIdx).trimEnd();
    if (cleaned.endsWith("---")) {
      cleaned = cleaned.slice(0, -3).trimEnd();
    }
  }
  if (cleaned.includes("# Session Context") && cleaned.includes("---")) {
    const tail = cleaned.split("---").pop()?.trim();
    if (tail) return tail;
  }
  if (cleaned.includes("---")) {
    cleaned = cleaned.split("---")[0].trim();
  }
  return cleaned;
}

export function stepsFromTurns(turns: Turn[]): Step[] {
  const steps: Step[] = [];
  let pendingGroup: { id: string; category: string; icon: LucideIcon; calls: ToolCall[] } | null =
    null;

  const flushGroup = () => {
    if (!pendingGroup || pendingGroup.calls.length === 0) return;
    steps.push({
      id: pendingGroup.id,
      type: "tool-group",
      icon: pendingGroup.icon,
      category: pendingGroup.category,
      summary: summarizeGroup(pendingGroup.category, pendingGroup.calls),
      calls: pendingGroup.calls,
    });
    pendingGroup = null;
  };

  // O(1) lookups for tool call tracking and step-by-id updates
  const toolInputById = new Map<string, ToolCall>();
  const terminalStepById = new Map<string, Step & { type: "terminal" }>();
  const contextGroupById = new Map<string, Step & { type: "context-group" }>();

  for (const turn of turns) {
    const turnId = turn.turn_id;

    // Phase label from user message
    const phase = parsePhaseLabel(turn.user_message || "");
    if (phase) {
      flushGroup();
      steps.push({ id: `phase:${turnId}:${phase}`, type: "phase", phase });
    }

    // Process events
    const events = turn.events || [];

    // User message — prefer the thread.message command event in the events array
    // (it has source/userId metadata). Only emit from turn.user_message as fallback.
    const hasCommandEvent = events.some(
      (e) => asRecord(e).type === "thread.message" && asRecord(e).message_type === "command",
    );
    if (!hasCommandEvent) {
      const userText = displayUserMessage(turn.user_message || "");
      if (userText) {
        flushGroup();
        steps.push({
          id: `user:turn-${turnId}`,
          type: "user-message",
          text: userText,
          userId: turn.user_id,
        });
      }
    }
    // Pre-scan: check if this turn has assistant text so we can skip
    // duplicate result events (the harness emits both).
    const turnHasAssistantText = events.some((raw) => {
      const e = asRecord(raw);
      if (asString(e.type) !== "assistant") return false;
      const content = (asRecord(e.message).content as unknown[]) || [];
      return content.some((b) => {
        const block = asRecord(b);
        return asString(block.type) === "text" && asString(block.text).trim();
      });
    });

    for (let ei = 0; ei < events.length; ei++) {
      const event = asRecord(events[ei]);
      const eventType = asString(event.type);

      if (eventType === "assistant") {
        const content = (asRecord(event.message).content as unknown[]) || [];
        for (let ci = 0; ci < content.length; ci++) {
          const block = asRecord(content[ci]);
          const blockType = asString(block.type);

          if (blockType === "text") {
            const text = asString(block.text).trim();
            if (!text) continue;
            flushGroup();
            steps.push({
              id: `result:turn-${turnId}-${ei}-${ci}`,
              type: "result",
              text,
              streaming: false,
            });
          } else if (blockType === "thinking") {
            const text = asString(block.thinking).trim();
            if (!text) continue;
            flushGroup();
            steps.push({ id: `thinking:turn-${turnId}-${ei}-${ci}`, type: "thinking", text });
          } else if (blockType === "tool_use") {
            const toolCallId =
              asString(block.id).trim() || `turn-${turnId}-tool-${ei}-${ci}`;
            const toolName = asString(block.name) || "tool";
            const input = asRecord(block.input);

            if (toolName === "str_replace") {
              flushGroup();
              const path = asString(input.path);
              const ext = path.split(".").pop()?.toLowerCase();
              steps.push({
                id: `diff:${toolCallId}`,
                type: "diff",
                file: path,
                lang: ext || "txt",
                oldStr: asString(input.old ?? input.old_str),
                newStr: asString(input.new ?? input.new_str),
              });
              // Track for potential output
              toolInputById.set(toolCallId, {
                id: toolCallId,
                name: toolName,
                input,
                state: "loading",
              });
              continue;
            }

            if (toolName === "shell" || toolName === "bash") {
              flushGroup();
              const termStep = {
                id: `terminal:${toolCallId}`,
                type: "terminal" as const,
                description: "Ran shell command",
                command: asString(input.command),
              };
              steps.push(termStep);
              terminalStepById.set(toolCallId, termStep);
              toolInputById.set(toolCallId, {
                id: toolCallId,
                name: toolName,
                input,
                state: "loading",
              });
              continue;
            }

            const call: ToolCall = {
              id: toolCallId,
              name: toolName,
              input,
              state: "loading",
            };
            toolInputById.set(toolCallId, call);

            const { icon, category } = categorizeToolCall(toolName);
            if (pendingGroup && pendingGroup.category === category) {
              pendingGroup.calls.push(call);
            } else {
              flushGroup();
              pendingGroup = {
                id: `tool-group:${toolCallId}:${category}`,
                category,
                icon,
                calls: [call],
              };
            }
          }
        }
      } else if (eventType === "tool") {
        const blocks = (event.content as unknown[]) || [];
        for (const rawBlock of blocks) {
          const block = asRecord(rawBlock);
          const toolCallId = asString(block.tool_use_id).trim();
          if (!toolCallId) continue;
          const tracked = toolInputById.get(toolCallId);
          if (tracked) {
            tracked.output = outputToText(block.content);
            tracked.state = "done";
            const terminalStep = terminalStepById.get(toolCallId);
            if (terminalStep) {
              terminalStep.output = tracked.output;
            }
          }
        }
      } else if (eventType === "reasoning") {
        const text = asString(event.text).trim();
        if (!text) continue;
        flushGroup();
        steps.push({ id: `thinking:turn-${turnId}-${ei}`, type: "thinking", text });
      } else if (eventType === "file_change") {
        flushGroup();
        const changesRaw = Array.isArray(event.changes) ? (event.changes as unknown[]) : [];
        const changes: Array<{ path: string; kind: "add" | "delete" | "update" }> = [];
        for (const raw of changesRaw) {
          const c = asRecord(raw);
          const path = asString(c.path);
          if (path) {
            changes.push({ path, kind: (asString(c.kind) as "add" | "delete" | "update") || "update" });
          }
        }
        if (changes.length > 0) {
          steps.push({
            id: `file-changes:turn-${turnId}-${ei}`,
            type: "file-changes",
            changes,
          });
        }
      } else if (eventType === "command_execution") {
        flushGroup();
        steps.push({
          id: `terminal:turn-${turnId}-${ei}`,
          type: "terminal",
          description: "Ran shell command",
          command: asString(event.command),
          output: asString(event.aggregated_output || event.output),
          exitCode: typeof event.exit_code === "number" ? (event.exit_code as number) : undefined,
        });
      } else if (eventType === "thread.message") {
        const messageType = asString(event.message_type);
        const text = asString(event.text).trim();
        if (!text) continue;

        if (messageType === "context") {
          flushGroup();
          const groupId = `context-group:${turnId}`;
          const item: ContextMessageItem = {
            id: asString(event.message_id) || `context:turn-${turnId}-${ei}`,
            text,
            source: asString(event.source) || undefined,
            userId: asString(event.user_id) || undefined,
            createdAt: asString(event.created_at) || undefined,
          };
          const existing = contextGroupById.get(groupId);
          if (existing) {
            existing.items.push(item);
          } else {
            const group = {
              id: groupId,
              type: "context-group" as const,
              title: "Thread discussion",
              items: [item],
            };
            steps.push(group);
            contextGroupById.set(groupId, group);
          }
        } else if (messageType === "command") {
          flushGroup();
          steps.push({
            id: asString(event.message_id) || `user:turn-${turnId}-${ei}`,
            type: "user-message",
            text,
            source: asString(event.source) || undefined,
            userId: asString(event.user_id) || undefined,
          });
        }
      } else if (eventType === "error") {
        flushGroup();
        steps.push({
          id: `error:turn-${turnId}-${ei}`,
          type: "error",
          message: asString(event.error || event.message),
        });
      } else if (eventType === "result") {
        // Skip result events when assistant text blocks already cover the same content.
        if (turnHasAssistantText) continue;
        const text = asString(event.result);
        if (text) {
          flushGroup();
          steps.push({
            id: `result:turn-${turnId}-${ei}`,
            type: "result",
            text,
            streaming: false,
          });
        }
      } else if (
        eventType === "item.started" ||
        eventType === "item.updated" ||
        eventType === "item.completed"
      ) {
        const item = asRecord(event.item);
        const itemType = asString(item.type);

        if (
          itemType === "mcp_tool_call" ||
          itemType === "tool_call" ||
          itemType === "function_call" ||
          itemType === "custom_tool_call"
        ) {
          const toolName =
            asString(item.tool || item.name || item.tool_name) || "tool";
          const toolInput = asRecord(item.arguments || item.input || item.args);
          const itemId =
            asString(item.id || item.tool_call_id || item.call_id) ||
            `turn-${turnId}-item-${ei}`;

          if (eventType === "item.started") {
            const call: ToolCall = {
              id: itemId,
              name: toolName,
              input: toolInput,
              state: "loading",
            };
            toolInputById.set(itemId, call);

            if (toolName === "str_replace") {
              flushGroup();
              const path = asString(toolInput.path);
              const ext = path.split(".").pop()?.toLowerCase();
              steps.push({
                id: `diff:${itemId}`,
                type: "diff",
                file: path,
                lang: ext || "txt",
                oldStr: asString(toolInput.old ?? toolInput.old_str),
                newStr: asString(toolInput.new ?? toolInput.new_str),
              });
              continue;
            }

            if (toolName === "shell" || toolName === "bash") {
              flushGroup();
              const termStep = {
                id: `terminal:${itemId}`,
                type: "terminal" as const,
                description: "Ran shell command",
                command: asString(toolInput.command),
              };
              steps.push(termStep);
              terminalStepById.set(itemId, termStep);
              continue;
            }

            const { icon, category } = categorizeToolCall(toolName);
            if (pendingGroup && pendingGroup.category === category) {
              pendingGroup.calls.push(call);
            } else {
              flushGroup();
              pendingGroup = {
                id: `tool-group:${itemId}:${category}`,
                category,
                icon,
                calls: [call],
              };
            }
          } else if (eventType === "item.completed") {
            let output = item.result;
            if (output === undefined && item.error !== undefined) output = item.error;
            const tracked = toolInputById.get(itemId);
            if (tracked) {
              tracked.output = outputToText(output);
              tracked.state = "done";
            }
            const terminalStep = terminalStepById.get(itemId);
            if (terminalStep) {
              terminalStep.output = outputToText(output);
            }
          }
        } else if (itemType === "command_execution" && eventType === "item.completed") {
          flushGroup();
          steps.push({
            id: `terminal:turn-${turnId}-item-${ei}`,
            type: "terminal",
            description: "Ran shell command",
            command: asString(item.command),
            output: asString(item.aggregated_output || item.output),
            exitCode: typeof item.exit_code === "number" ? (item.exit_code as number) : undefined,
          });
        } else if (
          itemType === "reasoning" &&
          (eventType === "item.updated" || eventType === "item.completed")
        ) {
          const text = asString(item.text || item.thinking);
          if (text) {
            flushGroup();
            steps.push({
              id: `thinking:turn-${turnId}-item-${ei}`,
              type: "thinking",
              text,
            });
          }
        } else if (eventType === "item.completed") {
          const text = asString(item.text);
          if (text) {
            flushGroup();
            steps.push({
              id: `result:turn-${turnId}-item-${ei}`,
              type: "result",
              text,
              streaming: false,
            });
          }
        }
      }
    }

    // Turn result as a final step (if not already captured by an event)
    if (turn.result) {
      const resultText = turn.result.trim();
      const alreadyHasResult = steps.some(
        (s) =>
          s.type === "result" &&
          s.text.trim() === resultText,
      );
      if (!alreadyHasResult) {
        flushGroup();
        steps.push({
          id: `result:turn-${turnId}-final`,
          type: "result",
          text: turn.result,
          streaming: false,
        });
      }
    }
  }

  flushGroup();
  return steps;
}
