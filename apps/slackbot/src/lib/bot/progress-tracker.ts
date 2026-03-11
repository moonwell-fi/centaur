import type { CanonicalEvent } from "@/lib/normalize-harness-event";

type ActiveTool = { name: string; input: Record<string, unknown>; startedAt: number };
type CompletedTool = { name: string; duration: number; isError: boolean };
type ActiveSubagent = {
  name: string;
  startedAt: number;
  activity: string;
  toolName: string;
};
type CompletedSubagent = { name: string; summary: string; status: string; duration: number };
type ActiveCommand = { command: string; startedAt: number };
type HandoffEntry = { goal: string; newThreadKey: string };
type FileChange = { file: string; action: string };
type UsageInfo = {
  inputTokens: number;
  outputTokens: number;
  model: string | null;
};

const TOOL_LABELS: Record<string, string> = {
  "websearch.deep_research": "Deep research",
  "websearch.search": "Web search",
  "slack.search_messages": "Searching Slack",
  "slack.get_message_files": "Fetching Slack files",
  "slack.upload_file": "Uploading to Slack",
  "slack.search_users": "Looking up Slack users",
  "crunchbase.search_organizations": "Searching Crunchbase",
  "crunchbase.search_people": "People search",
  "crunchbase.get_person": "Person lookup",
  "crunchbase.autocomplete": "Crunchbase lookup",
  "paradigmdb.notes_for_org": "Internal notes",
  "paradigmdb.notes_search": "Searching internal notes",
  "paradigmdb.db_people": "People directory",
  "paradigmdb.db_person": "Person lookup",
  "paradigmdb.db_organizations": "Organization lookup",
  "twitter.get_user": "Twitter profile",
  "twitter.get_followers": "Twitter followers",
  "twitter.get_following": "Discovering team via follows",
  "twitter.search_tweets": "Searching Twitter",
  "twitter.get_timeline": "Founder timeline",
  "twitter.lookup_users": "Looking up users",
  "googlenews.search": "News search",
  "archiver.extract_source": "Extracting document",
  "archiver.extract_files": "Parsing files",
  "archiver.extract_slack_files": "Parsing Slack uploads",
  "investmemos.search_memos": "Searching memos",
  "investmemos.build_miq_context": "Building memo context",
  "investmemos.read_memo": "Reading memo",
  "sensortower.search_apps": "App store data",
  "sensortower.get_app_info": "App analytics",
  "similarweb.get_traffic_overview": "Web traffic data",
  "similarweb.get_visits": "Website visits",
  "defillama.get_protocol": "DeFi protocol data",
  "defillama.get_tvl": "TVL data",
  "coingecko.get_coin": "Token data",
  "coinmetrics.get_metrics": "Onchain metrics",
  "dune.execute_query": "Running Dune query",
  "dune.get_results": "Dune results",
  "eodhd.get_fundamentals": "Financial data",
  "eodhd.get_eod": "Price history",
  "debank.get_portfolio": "Wallet portfolio",
  "nansen.get_wallet": "Wallet analysis",
  "nansen.get_address_labels": "Address labels",
  "nansen.get_smart_money_holdings": "Smart money holdings",
  "allium.query": "Onchain query",
  "harmonic.search_companies_natural_language": "Searching companies",
  "harmonic.enrich_company": "Company enrichment",
  "harmonic.enrich_person": "Person enrichment",
  "harmonic.get_similar_companies": "Finding similar companies",
  "messari.get_asset": "Asset data",
  "messari.get_asset_metrics": "Asset metrics",
  "messari.get_timeseries": "Asset time series",
  "newsapi.search": "News search",
  "newsapi.headlines": "Headlines",
  "token-terminal.get_project": "Protocol project data",
  "token-terminal.get_project_metrics": "Protocol metrics",
  "token-terminal.get_financial_statement": "Protocol financials",
  "tokenomist.get_unlock_events": "Token unlocks",
  "tokenomist.get_daily_emissions": "Token emissions",
  "tokenomist.get_allocations": "Token allocations",
  "tokenomist.get_fundraising": "Fundraising data",
  "standard-metrics.get_company": "Portfolio data",
  "standard-metrics.get_metrics": "Company metrics",
  "etherscan.get_transactions": "Onchain transactions",
  "etherscan.get_token_transfers": "Token transfers",
  "etherscan.get_balance": "Address balance",
  "arkham.get_entity": "Entity intelligence",
  "arkham.get_transfers": "Transfer tracking",
  "databento.get_stock_prices": "Stock prices",
  "coindesk.search": "Crypto news",
  "theblock.search": "Crypto news",
  "theblock.news": "Latest crypto news",
};

function humanizeToolName(raw: string): string {
  const exact = TOOL_LABELS[raw];
  if (exact) return exact;

  const [group, method] = raw.split(".", 2);
  if (method) {
    const methodLabel = method
      .replace(/_/g, " ")
      .replace(/^(get|list|search|fetch|read)\s+/, "");
    const groupLabel = group.charAt(0).toUpperCase() + group.slice(1);
    return `${groupLabel}: ${methodLabel}`;
  }

  return raw.replace(/_/g, " ");
}

function humanizeToolGroup(raw: string): string {
  const group = raw.split(".")[0];
  const labels: Record<string, string> = {
    websearch: "web search",
    slack: "Slack",
    crunchbase: "Crunchbase",
    paradigmdb: "internal DB",
    twitter: "Twitter",
    googlenews: "news",
    archiver: "document extraction",
    investmemos: "memo corpus",
    sensortower: "app analytics",
    similarweb: "web analytics",
    defillama: "DeFi data",
    coingecko: "token data",
    coinmetrics: "onchain metrics",
    dune: "Dune",
    eodhd: "financial data",
    harmonic: "company data",
    messari: "crypto data",
    newsapi: "news",
    "token-terminal": "protocol data",
    tokenomist: "token data",
    "standard-metrics": "portfolio data",
    etherscan: "onchain data",
    arkham: "address intelligence",
    databento: "market data",
    coindesk: "crypto news",
    theblock: "crypto news",
    nansen: "onchain analytics",
  };
  return labels[group] || group;
}

export class ProgressTracker {
  activeTools = new Map<string, ActiveTool>();
  completedTools: CompletedTool[] = [];
  activeSubagents = new Map<string, ActiveSubagent>();
  completedSubagents: CompletedSubagent[] = [];
  activeCommands = new Map<string, ActiveCommand>();
  handoffs: HandoffEntry[] = [];
  fileChanges: FileChange[] = [];
  reasoningText = "";
  usage: UsageInfo = { inputTokens: 0, outputTokens: 0, model: null };
  errorText = "";
  lastAssistantText = "";
  resultText = "";
  phase: "starting" | "working" | "done" = "starting";
  private startedAt = Date.now();
  private totalSubagentsLaunched = 0;

  update(event: CanonicalEvent): boolean {
    let changed = false;

    if (event.type === "assistant" && event.message?.content) {
      for (const block of event.message.content) {
        if (block.type === "tool_use") {
          this.activeTools.set(block.id, {
            name: block.name,
            input: block.input,
            startedAt: Date.now(),
          });
          this.lastAssistantText = "";
          this.phase = "working";
          changed = true;
        } else if (block.type === "text" && block.text) {
          if (this.lastAssistantText.length < 3000) {
            this.lastAssistantText += block.text;
          }
          changed = true;
        }
      }
      if (event.message.usage) {
        this.mergeUsage(event.message.usage, event.message.model);
      }
    } else if (event.type === "tool" && event.content) {
      for (const block of event.content) {
        const active = this.activeTools.get(block.tool_use_id);
        if (active) {
          this.activeTools.delete(block.tool_use_id);
          this.completedTools.push({
            name: active.name,
            duration: (Date.now() - active.startedAt) / 1000,
            isError: block.is_error,
          });
          changed = true;
        }
      }
    } else if (event.type === "reasoning") {
      this.reasoningText = event.text || "";
      this.phase = "working";
      changed = true;
    } else if (event.type === "subagent") {
      const id = event.subagent_id;
      if (event.status === "started") {
        this.activeSubagents.set(id, {
          name: event.name || "Subagent",
          startedAt: Date.now(),
          activity: "",
          toolName: "",
        });
        this.totalSubagentsLaunched += 1;
        this.phase = "working";
        changed = true;
      } else if (event.status === "working") {
        const sub = this.activeSubagents.get(id);
        if (sub) {
          sub.activity = event.activity || sub.activity;
          sub.toolName = event.tool_name || sub.toolName;
          changed = true;
        }
      } else if (event.status === "completed" || event.status === "failed") {
        const sub = this.activeSubagents.get(id);
        const duration = sub ? (Date.now() - sub.startedAt) / 1000 : 0;
        this.activeSubagents.delete(id);
        this.completedSubagents.push({
          name: event.name || sub?.name || "Subagent",
          summary: event.summary || "",
          status: event.status,
          duration,
        });
        changed = true;
      }
    } else if (event.type === "command_execution") {
      const id = `cmd-${Date.now()}`;
      this.activeCommands.set(id, {
        command: event.command,
        startedAt: Date.now(),
      });
      this.phase = "working";
      setTimeout(() => this.activeCommands.delete(id), 100);
      changed = true;
    } else if (event.type === "file_change") {
      for (const change of event.changes ?? []) {
        const c = change as Record<string, unknown>;
        const file = String(c.file || c.path || c.filename || "");
        const action = String(c.action || c.type || c.status || "modified");
        if (file) {
          this.fileChanges.push({ file, action });
        }
      }
      this.phase = "working";
      changed = true;
    } else if (event.type === "usage") {
      this.mergeUsage(event.usage, event.model);
      changed = true;
    } else if (event.type === "result") {
      this.resultText = event.text;
      this.phase = "done";
      changed = true;
    } else if (event.type === "error") {
      this.errorText = event.error || "";
      this.phase = "done";
      changed = true;
    }

    return changed;
  }

  private mergeUsage(usage: Record<string, unknown>, model?: string | null): void {
    const input = toNonNegativeInt(usage.input_tokens);
    const output = toNonNegativeInt(usage.output_tokens);
    if (input > 0) this.usage.inputTokens += input;
    if (output > 0) this.usage.outputTokens += output;
    if (model) this.usage.model = model;
  }

  addHandoff(goal: string, newThreadKey: string): void {
    this.handoffs.push({ goal, newThreadKey });
    this.phase = "working";
    this.activeTools.clear();
    this.activeCommands.clear();
    this.activeSubagents.clear();
    this.lastAssistantText = "";
    this.resultText = "";
    this.reasoningText = "";
    this.errorText = "";
  }

  private elapsedLabel(): string {
    return formatDuration((Date.now() - this.startedAt) / 1000);
  }

  toSlackBullets(): string {
    if (this.phase === "done") {
      const parts: string[] = [];
      const groups = [
        ...new Set(this.completedTools.map((t) => humanizeToolGroup(t.name))),
      ];
      if (groups.length > 0) parts.push(groups.join(", "));
      if (this.completedSubagents.length > 0) {
        parts.push(`${this.completedSubagents.length} research track${this.completedSubagents.length === 1 ? "" : "s"}`);
      }
      if (this.fileChanges.length > 0) {
        parts.push(`${this.fileChanges.length} file ${this.fileChanges.length === 1 ? "change" : "changes"}`);
      }
      if (this.usage.inputTokens + this.usage.outputTokens > 0) {
        parts.push(formatTokens(this.usage.inputTokens + this.usage.outputTokens));
      }
      const suffix = parts.length > 0 ? ` — ${parts.join(" · ")}` : "";
      if (this.errorText) {
        return `❌ Failed (${this.elapsedLabel()})${suffix}\n• ${this.errorText}`;
      }
      return `✅ Done (${this.elapsedLabel()})${suffix}`;
    }

    const lines: string[] = [];

    const headerParts: string[] = [];
    if (this.totalSubagentsLaunched > 0) {
      const done = this.completedSubagents.length;
      const total = this.totalSubagentsLaunched;
      headerParts.push(`${done}/${total} research tracks`);
    }
    headerParts.push(this.elapsedLabel());
    lines.push(`⏳ *Working* (${headerParts.join(" · ")})`);

    for (const ho of this.handoffs) {
      lines.push(`  🔀 Handed off → _${ho.goal}_`);
    }

    if (this.reasoningText) {
      lines.push(`  💭 _Thinking…_`);
    }

    for (const [, tool] of this.activeTools) {
      const label = humanizeToolName(tool.name);
      const context = describeToolContext(tool.name, tool.input);
      const suffix = context ? ` — ${context}` : "";
      lines.push(`  🔧 ${label}${suffix}`);
    }

    for (const [, sub] of this.activeSubagents) {
      const elapsed = formatDuration((Date.now() - sub.startedAt) / 1000);
      let line = `  🔍 _${sub.name}_ (${elapsed})`;
      if (sub.activity) {
        line += ` — ${clipText(sub.activity, 80)}`;
      } else if (sub.toolName) {
        line += ` — ${humanizeToolName(sub.toolName)}`;
      }
      lines.push(line);
    }

    for (const [, cmd] of this.activeCommands) {
      lines.push(`  💻 \`${cmd.command}\``);
    }

    for (const sub of this.completedSubagents) {
      const icon = sub.status === "failed" ? "❌" : "✅";
      const dur = formatDuration(sub.duration);
      let line = `  ${icon} _${sub.name}_ (${dur})`;
      if (sub.summary && sub.status !== "failed") {
        line += ` — ${clipText(sub.summary, 100)}`;
      }
      lines.push(line);
    }

    const recentTools = this.completedTools.slice(-4);
    for (const tool of recentTools) {
      const icon = tool.isError ? "❌" : "✓";
      lines.push(`  ${icon} ${humanizeToolName(tool.name)} (${tool.duration.toFixed(1)}s)`);
    }
    if (this.completedTools.length > 4) {
      const hidden = this.completedTools.length - 4;
      lines.push(`  _+${hidden} more_`);
    }

    if (this.fileChanges.length > 0) {
      const actionCounts = new Map<string, number>();
      for (const fc of this.fileChanges) {
        actionCounts.set(fc.action, (actionCounts.get(fc.action) || 0) + 1);
      }
      const summary = [...actionCounts.entries()]
        .map(([action, count]) => `${count} ${action}`)
        .join(", ");
      lines.push(`  📝 ${summary}`);
    }

    if (this.lastAssistantText && this.activeTools.size === 0 && this.activeSubagents.size === 0) {
      const preview = truncatePreview(this.lastAssistantText, 600);
      if (preview) {
        lines.push("");
        lines.push(preview);
      }
    }

    return lines.join("\n");
  }
}

function toNonNegativeInt(value: unknown): number {
  if (typeof value === "number" && value >= 0) return Math.floor(value);
  return 0;
}

function formatDuration(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  const m = Math.floor(seconds / 60);
  const s = Math.floor(seconds % 60);
  return s > 0 ? `${m}m ${s}s` : `${m}m`;
}

function formatTokens(total: number): string {
  if (total >= 1_000_000) return `${(total / 1_000_000).toFixed(1)}M tokens`;
  if (total >= 1_000) return `${(total / 1_000).toFixed(1)}k tokens`;
  return `${total} tokens`;
}

function clipText(value: string, maxChars: number): string {
  const cleaned = value.replace(/\s+/g, " ").trim();
  if (cleaned.length <= maxChars) return cleaned;
  return cleaned.slice(0, maxChars - 1) + "…";
}

function truncatePreview(text: string, maxChars: number): string {
  const trimmed = text.trim();
  if (!trimmed || trimmed.length < 20) return "";
  if (trimmed.length <= maxChars) return trimmed;
  const cut = trimmed.slice(0, maxChars);
  const lastNewline = cut.lastIndexOf("\n");
  const breakAt = lastNewline > maxChars * 0.5 ? lastNewline : cut.lastIndexOf(" ");
  return (breakAt > 0 ? cut.slice(0, breakAt) : cut) + " …";
}

function describeToolContext(toolName: string, input: Record<string, unknown>): string {
  for (const key of ["query", "question", "prompt", "message"]) {
    const val = input[key];
    if (typeof val === "string" && val.trim()) {
      return `"${clipText(val, 70)}"`;
    }
  }
  if (typeof input.source_url === "string") return clipText(input.source_url, 60);
  if (typeof input.url === "string") return clipText(input.url, 60);
  if (typeof input.username === "string") return `@${input.username}`;
  if (typeof input.org_name === "string") return input.org_name;
  if (typeof input.name === "string") return input.name;
  if (typeof input.memo === "string") return input.memo;
  if (typeof input.path === "string") return clipText(input.path, 50);
  if (typeof input.command === "string") return `\`${clipText(input.command, 50)}\``;
  return "";
}
