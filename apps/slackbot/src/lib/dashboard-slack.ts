import type {
  DashboardSpec,
  DashboardComponent,
  CellFormat,
} from "./dashboard-types";
import { extractDashboardBlocks } from "./dashboard-parser";

const currencyFmt = new Intl.NumberFormat("en-US", {
  style: "currency",
  currency: "USD",
  minimumFractionDigits: 0,
  maximumFractionDigits: 0,
});

const numberFmt = new Intl.NumberFormat("en-US", {
  maximumFractionDigits: 2,
});

function fmt(value: unknown, format: CellFormat): string {
  if (value == null) return "—";
  switch (format) {
    case "currency": {
      const n = Number(value);
      return isNaN(n) ? String(value) : currencyFmt.format(n);
    }
    case "percent": {
      let n = Number(value);
      if (isNaN(n)) return String(value);
      if (Math.abs(n) < 1) n = n * 100;
      const sign = n > 0 ? "+" : "";
      return `${sign}${n.toFixed(1)}%`;
    }
    case "number": {
      const n = Number(value);
      return isNaN(n) ? String(value) : numberFmt.format(n);
    }
    case "date": {
      const d = new Date(value as string | number);
      return isNaN(d.getTime()) ? String(value) : d.toLocaleDateString();
    }
    default:
      return String(value);
  }
}

function renderComponent(c: DashboardComponent): string {
  switch (c.type) {
    case "kpi-card": {
      const val = fmt(c.value, c.format);
      const delta = c.delta != null ? ` (${c.delta >= 0 ? "↑" : "↓"}${Math.abs(c.delta).toFixed(1)}%)` : "";
      return `*${c.label}:* ${val}${delta}`;
    }
    case "data-table": {
      const lines: string[] = [];
      if (c.title) lines.push(`*${c.title}*`);
      const header = c.columns.map((col) => col.label).join(" | ");
      const sep = c.columns.map(() => "---").join(" | ");
      lines.push(header, sep);
      for (const row of c.data.slice(0, 15)) {
        lines.push(c.columns.map((col) => fmt(row[col.key], col.format)).join(" | "));
      }
      if (c.data.length > 15) lines.push(`_...and ${c.data.length - 15} more rows_`);
      return lines.join("\n");
    }
    case "line-chart":
    case "bar-chart":
    case "pie-chart": {
      const lines: string[] = [`*${c.title}*`];
      const rows = c.data.slice(0, 10);
      if (c.type === "line-chart") {
        for (const row of rows) {
          const vals = c.yKeys.map((k) => String(row[k] ?? "—")).join(", ");
          lines.push(`${row[c.xKey]} → ${vals}`);
        }
      } else if (c.type === "bar-chart") {
        for (const row of rows) {
          lines.push(`${row[c.categoryKey]}: ${row[c.valueKey]}`);
        }
      } else {
        for (const row of rows) {
          lines.push(`${row[c.labelKey]}: ${row[c.valueKey]}`);
        }
      }
      if (c.data.length > 10) lines.push(`_...and ${c.data.length - 10} more_`);
      return lines.join("\n");
    }
    default:
      return "";
  }
}

function specToSlack(spec: DashboardSpec): string {
  const lines: string[] = [`*${spec.title}*`];

  const kpis = spec.components.filter((c) => c.type === "kpi-card");
  const rest = spec.components.filter((c) => c.type !== "kpi-card");

  if (kpis.length > 0) {
    lines.push(kpis.map((c) => renderComponent(c)).join("  ·  "));
  }

  for (const c of rest) {
    lines.push("", renderComponent(c));
  }

  return lines.join("\n");
}

const DASHBOARD_REGEX = /```dashboard\n([\s\S]*?)```/g;

export function renderDashboardsForSlack(markdown: string): string {
  const blocks = extractDashboardBlocks(markdown);
  if (blocks.length === 0) return markdown;

  let result = "";
  for (const block of blocks) {
    result += block.before;
    result += specToSlack(block.spec);
    result += block.after;
  }
  return result;
}
