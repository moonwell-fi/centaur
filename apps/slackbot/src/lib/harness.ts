/**
 * Agent API client — calls the agent plugin via the AI v2 REST API.
 *
 * spawn() → creates a Docker container for the thread
 * execute() → runs a message in the container, returns result text
 */

const API_URL = process.env.AI_V2_API_URL || "http://api:8000";
const API_KEY = process.env.AI_V2_API_KEY || "";

async function agentCall(
  endpoint: string,
  args: Record<string, unknown>
): Promise<Record<string, unknown>> {
  const res = await fetch(`${API_URL}/agent/${endpoint}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(args),
  });

  if (!res.ok) {
    const text = await res.text();
    throw new Error(`agent/${endpoint} failed (${res.status}): ${text}`);
  }

  return await res.json();
}

export type Harness = "amp" | "claude-code" | "codex";

/** Parse "harness=amp" directive from message text. */
export function extractHarness(text: string): {
  harness: Harness;
  cleanedText: string;
} {
  const match = text.match(/\bharness\s*=\s*(amp|claude-code|codex)\b/i);
  if (match) {
    const harness = match[1].toLowerCase() as Harness;
    const cleanedText = (
      text.slice(0, match.index) + text.slice(match.index! + match[0].length)
    ).trim();
    return { harness, cleanedText };
  }
  return { harness: "amp", cleanedText: text };
}

/** Spawn a container for a Slack thread (idempotent). */
export async function spawn(
  threadKey: string,
  harness: Harness = "amp",
  repo?: string
): Promise<{ sessionId: string; status: string }> {
  const result = await agentCall("spawn", {
    slack_thread_key: threadKey,
    harness,
    ...(repo ? { repo } : {}),
  });
  return {
    sessionId: result.session_id as string,
    status: result.status as string,
  };
}

/** Execute a message and return the final result text. Auto-spawns if needed. */
export async function execute(
  threadKey: string,
  message: string,
  harness: Harness = "amp"
): Promise<string> {
  const result = await agentCall("execute", {
    slack_thread_key: threadKey,
    message,
    harness,
  });
  return (result.result as string) || "No response from agent.";
}

/** Stop a session. */
export async function stop(threadKey: string): Promise<void> {
  await agentCall("stop", { slack_thread_key: threadKey });
}

/** Interrupt a running command. */
export async function interrupt(threadKey: string): Promise<void> {
  await agentCall("interrupt", { slack_thread_key: threadKey });
}
