"use client";

import { useEffect, useState, useMemo, useCallback } from "react";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Loader2Icon, ArrowUpDown, ArrowUp, ArrowDown, AlertCircleIcon } from "lucide-react";

interface Position {
  assetName: string;
  ticker: string;
  marketValue: number;
  investedCapital: number;
  moic: number;
  price: number;
  priceChange24h: number;
  eodDate: string;
  fundName: string;
  status: string;
  [key: string]: unknown;
}

type SortKey = "assetName" | "marketValue" | "investedCapital" | "moic" | "price";
type SortDir = "asc" | "desc";

function formatCompact(value: number): string {
  const abs = Math.abs(value);
  if (abs >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(1)}B`;
  if (abs >= 1_000_000) return `$${(value / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `$${(value / 1_000).toFixed(1)}K`;
  return `$${value.toFixed(0)}`;
}

function formatCurrency(value: number | null | undefined): string {
  if (value == null || isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 0,
    maximumFractionDigits: 0,
  }).format(value);
}

function formatPrice(value: number | null | undefined): string {
  if (value == null || isNaN(value)) return "—";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  }).format(value);
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || isNaN(value)) return "—";
  const sign = value > 0 ? "+" : "";
  return `${sign}${(value * 100).toFixed(2)}%`;
}

function SortIcon({ column, sortKey, sortDir }: { column: SortKey; sortKey: SortKey; sortDir: SortDir }) {
  if (sortKey !== column) return <ArrowUpDown className="ml-1 inline size-3 text-muted-foreground/50" />;
  return sortDir === "asc"
    ? <ArrowUp className="ml-1 inline size-3" />
    : <ArrowDown className="ml-1 inline size-3" />;
}

export default function PortfolioPage() {
  const [positions, setPositions] = useState<Position[]>([]);
  const [funds, setFunds] = useState<string[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [search, setSearch] = useState("");
  const [fundFilter, setFundFilter] = useState("all");
  const [sortKey, setSortKey] = useState<SortKey>("marketValue");
  const [sortDir, setSortDir] = useState<SortDir>("desc");

  useEffect(() => {
    async function load() {
      setLoading(true);
      setError(null);
      try {
        const [posRes, fundRes] = await Promise.all([
          fetch("/api/portfolio/positions?limit=200"),
          fetch("/api/portfolio/funds"),
        ]);

        if (!posRes.ok) throw new Error(`Positions API error: ${posRes.status}`);

        const posData = await posRes.json();
        const parsed: Position[] = (() => {
          const raw = posData.result;
          if (!raw) return [];
          const arr = typeof raw === "string" ? JSON.parse(raw) : raw;
          return Array.isArray(arr) ? arr : [];
        })();
        setPositions(parsed);

        if (fundRes.ok) {
          const fundData = await fundRes.json();
          const rawFunds = fundData.result;
          const fundArr = typeof rawFunds === "string" ? JSON.parse(rawFunds) : rawFunds;
          if (Array.isArray(fundArr)) {
            const names = fundArr
              .map((f: Record<string, unknown>) => (f.fundName ?? f.name ?? "") as string)
              .filter(Boolean);
            setFunds([...new Set(names)].sort());
          }
        }
      } catch (err) {
        setError(err instanceof Error ? err.message : "Failed to load portfolio data");
      } finally {
        setLoading(false);
      }
    }
    load();
  }, []);

  const toggleSort = useCallback((key: SortKey) => {
    setSortKey((prev) => {
      if (prev === key) {
        setSortDir((d) => (d === "asc" ? "desc" : "asc"));
        return prev;
      }
      setSortDir(key === "assetName" ? "asc" : "desc");
      return key;
    });
  }, []);

  const filtered = useMemo(() => {
    let rows = positions;
    if (fundFilter !== "all") {
      rows = rows.filter((p) => p.fundName === fundFilter);
    }
    if (search) {
      const q = search.toLowerCase();
      rows = rows.filter(
        (p) =>
          p.assetName?.toLowerCase().includes(q) ||
          p.ticker?.toLowerCase().includes(q),
      );
    }
    return rows;
  }, [positions, search, fundFilter]);

  const sorted = useMemo(() => {
    return [...filtered].sort((a, b) => {
      const av = a[sortKey];
      const bv = b[sortKey];
      if (av == null && bv == null) return 0;
      if (av == null) return 1;
      if (bv == null) return -1;
      const cmp =
        typeof av === "number" && typeof bv === "number"
          ? av - bv
          : String(av).localeCompare(String(bv));
      return sortDir === "asc" ? cmp : -cmp;
    });
  }, [filtered, sortKey, sortDir]);

  const totalMarketValue = useMemo(
    () => filtered.reduce((sum, p) => sum + (p.marketValue ?? 0), 0),
    [filtered],
  );
  const totalInvestedCapital = useMemo(
    () => filtered.reduce((sum, p) => sum + (p.investedCapital ?? 0), 0),
    [filtered],
  );

  if (loading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Loader2Icon className="size-6 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-full items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-center">
          <AlertCircleIcon className="size-8 text-destructive" />
          <p className="text-sm text-muted-foreground">{error}</p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="rounded-md bg-secondary px-3 py-1.5 text-xs font-medium text-foreground hover:bg-secondary/80"
          >
            Retry
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="mx-auto max-w-[1400px] px-6 py-6">
      {/* Header */}
      <div className="mb-6 flex items-start justify-between">
        <h1 className="text-2xl font-semibold tracking-tight">Positions</h1>
        <div className="flex items-center gap-6">
          <div className="text-right">
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Market Value
            </p>
            <p className="text-lg font-semibold tabular-nums">{formatCompact(totalMarketValue)}</p>
          </div>
          <div className="text-right">
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Invested Capital
            </p>
            <p className="text-lg font-semibold tabular-nums">
              {formatCompact(totalInvestedCapital)}
            </p>
          </div>
          <div className="text-right">
            <p className="text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
              Positions
            </p>
            <p className="text-lg font-semibold tabular-nums">{filtered.length}</p>
          </div>
        </div>
      </div>

      {/* Filter bar */}
      <div className="mb-4 flex items-center gap-3">
        <Input
          type="search"
          placeholder="Search by name or ticker…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          className="h-8 w-64 border-border bg-background px-2.5 text-sm shadow-none focus-visible:ring-1"
        />
        <Select value={fundFilter} onValueChange={setFundFilter}>
          <SelectTrigger size="sm" className="w-48">
            <SelectValue placeholder="All Funds" />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="all">All Funds</SelectItem>
            {funds.map((f) => (
              <SelectItem key={f} value={f}>
                {f}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {/* Table */}
      {sorted.length === 0 ? (
        <div className="flex h-64 items-center justify-center rounded-md border border-border bg-card">
          <p className="text-sm text-muted-foreground">No positions found</p>
        </div>
      ) : (
        <div className="overflow-hidden rounded-md border border-border bg-card">
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b border-border bg-muted/30">
                  <th
                    onClick={() => toggleSort("assetName")}
                    className="cursor-pointer select-none px-4 py-2.5 text-left text-xs font-medium text-muted-foreground hover:text-foreground"
                  >
                    Position
                    <SortIcon column="assetName" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th
                    onClick={() => toggleSort("price")}
                    className="cursor-pointer select-none px-4 py-2.5 text-right text-xs font-medium text-muted-foreground hover:text-foreground"
                  >
                    Price
                    <SortIcon column="price" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th
                    onClick={() => toggleSort("marketValue")}
                    className="cursor-pointer select-none px-4 py-2.5 text-right text-xs font-medium text-muted-foreground hover:text-foreground"
                  >
                    Market Value
                    <SortIcon column="marketValue" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th
                    onClick={() => toggleSort("investedCapital")}
                    className="cursor-pointer select-none px-4 py-2.5 text-right text-xs font-medium text-muted-foreground hover:text-foreground"
                  >
                    Invested Capital
                    <SortIcon column="investedCapital" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                  <th
                    onClick={() => toggleSort("moic")}
                    className="cursor-pointer select-none px-4 py-2.5 text-right text-xs font-medium text-muted-foreground hover:text-foreground"
                  >
                    MOIC
                    <SortIcon column="moic" sortKey={sortKey} sortDir={sortDir} />
                  </th>
                </tr>
              </thead>
              <tbody>
                {sorted.map((pos, i) => (
                  <tr
                    key={`${pos.ticker}-${pos.fundName}-${i}`}
                    className="border-b border-border/40 last:border-0 transition-colors hover:bg-muted/30"
                  >
                    {/* Position */}
                    <td className="px-4 py-2.5">
                      <div className="flex flex-col">
                        <span className="font-medium text-foreground">
                          {pos.assetName || "—"}
                        </span>
                        <span className="text-[11px] text-muted-foreground">
                          {pos.ticker || ""}
                          {pos.fundName ? ` · ${pos.fundName}` : ""}
                        </span>
                      </div>
                    </td>
                    {/* Price */}
                    <td className="px-4 py-2.5 text-right">
                      <div className="flex flex-col items-end">
                        <span className="tabular-nums text-foreground">
                          {formatPrice(pos.price)}
                        </span>
                        {pos.priceChange24h != null && !isNaN(pos.priceChange24h) && (
                          <span
                            className={`text-[11px] tabular-nums font-medium ${
                              pos.priceChange24h >= 0
                                ? "text-primary"
                                : "text-destructive"
                            }`}
                          >
                            {formatPercent(pos.priceChange24h)}
                          </span>
                        )}
                      </div>
                    </td>
                    {/* Market Value */}
                    <td className="px-4 py-2.5 text-right tabular-nums text-foreground">
                      {formatCurrency(pos.marketValue)}
                    </td>
                    {/* Invested Capital */}
                    <td className="px-4 py-2.5 text-right tabular-nums text-foreground">
                      {formatCurrency(pos.investedCapital)}
                    </td>
                    {/* MOIC */}
                    <td className="px-4 py-2.5 text-right">
                      <span
                        className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium tabular-nums ${
                          pos.moic != null && pos.moic >= 1.0
                            ? "bg-primary/10 text-primary"
                            : "bg-destructive/10 text-destructive"
                        }`}
                      >
                        {pos.moic != null ? `${pos.moic.toFixed(2)}x` : "—"}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}
    </div>
  );
}
