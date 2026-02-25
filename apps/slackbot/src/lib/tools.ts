/**
 * AI tools that proxy to the AI v2 API layer.
 *
 * These are registered as Vercel AI SDK tools so Claude can call
 * search, sql_query, list_plugins, describe_plugin, and call_plugin.
 */

import { tool } from "ai";
import { z } from "zod";

const API_URL = process.env.AI_V2_API_URL || "http://api:8000";
const API_KEY = process.env.AI_V2_API_KEY || "";

async function apiPost(path: string, body: Record<string, unknown>): Promise<string> {
  const res = await fetch(`${API_URL}${path}`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${path} failed (${res.status}): ${text}`);
  }
  const data = await res.json();
  return typeof data === "string" ? data : JSON.stringify(data);
}

async function apiGet(path: string): Promise<string> {
  const res = await fetch(`${API_URL}${path}`, {
    headers: {
      Authorization: `Bearer ${API_KEY}`,
      "Content-Type": "application/json",
    },
  });
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${path} failed (${res.status}): ${text}`);
  }
  const data = await res.json();
  return typeof data === "string" ? data : JSON.stringify(data);
}

export const searchTool = tool({
  description:
    "Hybrid semantic + keyword search across all ingested data (Slack, Linear, GitHub, GCal, Gmail, etc.)",
  inputSchema: z.object({
    query: z.string().describe("Search query"),
    sources: z
      .array(z.string())
      .optional()
      .describe("Filter to specific sources (e.g. ['slack', 'linear'])"),
    limit: z.number().optional().default(10).describe("Max results"),
  }),
  execute: async ({ query, sources, limit }) => {
    const params = new URLSearchParams({ q: query, limit: String(limit ?? 10) });
    if (sources?.length) params.set("sources", sources.join(","));
    return apiGet(`/search?${params}`);
  },
});

export const sqlQueryTool = tool({
  description:
    "Run a read-only SQL query against the Postgres database (raw_records JSONB, embeddings, sync_cursors tables)",
  inputSchema: z.object({
    query: z.string().describe("SQL query (read-only, no INSERT/UPDATE/DELETE)"),
  }),
  execute: async ({ query }) => {
    return apiPost("/query", { query });
  },
});

export const listPluginsTool = tool({
  description:
    "List all available plugins and their tool names. Call this to discover what data sources and capabilities are available.",
  inputSchema: z.object({}),
  execute: async () => {
    return apiGet("/plugins");
  },
});

export const callPluginTool = tool({
  description:
    "Call a plugin tool. Use list_plugins first to discover available plugins, then call with plugin name, tool name, and arguments. Examples: call_plugin('coingecko', 'get_price', {id: 'ethereum'}), call_plugin('slack', 'search_messages', {query: 'reth'})",
  inputSchema: z.object({
    plugin: z.string().describe("Plugin name (e.g. 'coingecko', 'slack', 'linear')"),
    tool: z.string().describe("Tool name within the plugin"),
    args: z
      .record(z.unknown())
      .optional()
      .default({})
      .describe("Arguments to pass to the tool"),
  }),
  execute: async ({ plugin, tool: toolName, args }) => {
    return apiPost(`/plugins/${plugin}/${toolName}`, args ?? {});
  },
});
