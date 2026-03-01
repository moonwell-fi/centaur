"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import {
  forwardRef,
  type KeyboardEvent as ReactKeyboardEvent,
  useCallback,
  useEffect,
  useId,
  useImperativeHandle,
  useMemo,
  useRef,
  useState,
} from "react";
import { ChevronLeft, ChevronRight, LoaderCircle, RefreshCw } from "lucide-react";
import { ParticipantAvatars } from "@/components/thread/participant-avatars";
import { Progress } from "@/components/ui/progress";
import { HarnessBadge } from "@/components/ui/harness-badge";
import { StateDot } from "@/components/ui/state-dot";
import { useElapsed } from "@/hooks/use-elapsed";
import { useLiveThreadStatus } from "@/hooks/use-live-thread-status";
import { BASE } from "@/lib/constants";
import { threadName } from "@/lib/thread-name";
import { PHASES, type ThreadSummary } from "@/lib/types";
import { cn } from "@/lib/utils";

type ThreadStatusFilter = "all" | "active" | "idle" | "error";

export type ThreadSidebarHandle = {
  focusSearch: () => void;
  focusSidebar: () => void;
};

type ThreadSidebarProps = {
  selectedThreadKey: string | null;
  collapsed: boolean;
  onCollapsedChange?: (collapsed: boolean) => void;
  onNavigate?: () => void;
  showCollapseToggle?: boolean;
  active?: boolean;
};

function parsePhaseFromMessage(message: string | undefined): string | null {
  const text = message ?? "";
  const match = text.match(/^\[([^\]]+)\]/);
  return match ? match[1].trim().toLowerCase() : null;
}

function parseActivePhase(thread: ThreadSummary): string | null {
  return parsePhaseFromMessage(thread.last_user_message) ?? parsePhaseFromMessage(thread.first_message);
}

function isRunningState(state: string): boolean {
  return state === "working" || state === "running";
}

function isTextInputTarget(target: EventTarget | null): boolean {
  return target instanceof HTMLElement && !!target.closest("input, textarea, select, [contenteditable='true']");
}

function runningSubtitle(thread: ThreadSummary): string | null {
  if (!isRunningState(thread.state)) return null;
  const phase = parseActivePhase(thread);
  if (phase) return `Working on ${phase}...`;
  return "Working...";
}

function ThreadAge({ thread }: { thread: ThreadSummary }) {
  const elapsed = useElapsed(thread.last_activity, isRunningState(thread.state));
  return <span>{elapsed}</span>;
}

export const ThreadSidebar = forwardRef<ThreadSidebarHandle, ThreadSidebarProps>(function ThreadSidebar(
  {
    selectedThreadKey,
    collapsed,
    onCollapsedChange,
    onNavigate,
    showCollapseToggle = true,
    active = true,
  },
  ref,
) {
  const router = useRouter();
  const [threads, setThreads] = useState<ThreadSummary[]>([]);
  const [loading, setLoading] = useState(true);
  const [isRefreshing, setIsRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [query, setQuery] = useState("");
  const [statusFilter, setStatusFilter] = useState<ThreadStatusFilter>("all");
  const [focusedThreadKey, setFocusedThreadKey] = useState<string | null>(null);
  const filterId = useId();
  const searchRef = useRef<HTMLInputElement>(null);
  const toggleRef = useRef<HTMLButtonElement>(null);
  const cardRefs = useRef<Record<string, HTMLAnchorElement | null>>({});
  const latestFetchIdRef = useRef(0);
  const detailPrefetchAtRef = useRef<Record<string, number>>({});

  const fetchThreads = useCallback(
    async (showRefreshIndicator = true) => {
      if (!active) return;
      const fetchId = latestFetchIdRef.current + 1;
      latestFetchIdRef.current = fetchId;
      if (showRefreshIndicator) setIsRefreshing(true);
      try {
        const res = await fetch(`${BASE}/api/threads`);
        if (!res.ok) {
          throw new Error(`threads fetch failed: ${res.status}`);
        }
        const data = (await res.json()) as { threads?: ThreadSummary[] };
        if (latestFetchIdRef.current !== fetchId) return;
        setThreads(Array.isArray(data.threads) ? data.threads : []);
        setError(null);
      } catch {
        if (latestFetchIdRef.current !== fetchId) return;
        setError("Unable to load threads.");
      } finally {
        if (latestFetchIdRef.current !== fetchId) return;
        setLoading(false);
        if (showRefreshIndicator) setIsRefreshing(false);
      }
    },
    [active],
  );

  useEffect(() => {
    if (!active) return;
    void fetchThreads(false);
    const interval = window.setInterval(() => {
      void fetchThreads(false);
    }, 5000);
    return () => window.clearInterval(interval);
  }, [active, fetchThreads]);

  const filteredThreads = useMemo(() => {
    const lowerQuery = query.trim().toLowerCase();
    return threads.filter((thread) => {
      if (statusFilter === "active" && !isRunningState(thread.state)) return false;
      if (statusFilter === "idle" && (isRunningState(thread.state) || thread.state === "error")) return false;
      if (statusFilter === "error" && thread.state !== "error") return false;
      if (!lowerQuery) return true;
      const haystack =
        `${thread.thread_name ?? ""} ${thread.first_message ?? ""} ${thread.last_result ?? ""} ${thread.slack_thread_key}`.toLowerCase();
      return haystack.includes(lowerQuery);
    });
  }, [query, statusFilter, threads]);

  const sortedThreads = useMemo(
    () =>
      [...filteredThreads].sort((a, b) => {
        const aActive = isRunningState(a.state) ? 1 : 0;
        const bActive = isRunningState(b.state) ? 1 : 0;
        if (aActive !== bActive) return bActive - aActive;
        return (b.last_activity ?? 0) - (a.last_activity ?? 0);
      }),
    [filteredThreads],
  );

  useEffect(() => {
    if (sortedThreads.length === 0) {
      setFocusedThreadKey(null);
      return;
    }
    const hasFocused = focusedThreadKey
      ? sortedThreads.some((thread) => thread.slack_thread_key === focusedThreadKey)
      : false;
    if (selectedThreadKey && sortedThreads.some((thread) => thread.slack_thread_key === selectedThreadKey)) {
      setFocusedThreadKey(selectedThreadKey);
      return;
    }
    if (!hasFocused) {
      setFocusedThreadKey(sortedThreads[0].slack_thread_key);
    }
  }, [focusedThreadKey, selectedThreadKey, sortedThreads]);

  const activeThreadKeys = useMemo(() => {
    if (!active || collapsed) return [];
    return sortedThreads
      .filter((thread) => isRunningState(thread.state))
      .slice(0, 8)
      .map((thread) => thread.slack_thread_key);
  }, [active, collapsed, sortedThreads]);
  const liveStatusByThread = useLiveThreadStatus(activeThreadKeys);

  const activeCount = useMemo(
    () => threads.filter((thread) => isRunningState(thread.state)).length,
    [threads],
  );

  const prefetchThread = useCallback(
    (threadKey: string) => {
      const href = `/threads/${encodeURIComponent(threadKey)}`;
      router.prefetch(href);

      const now = Date.now();
      const previousAt = detailPrefetchAtRef.current[threadKey] ?? 0;
      if (now - previousAt < 15000) return;
      detailPrefetchAtRef.current[threadKey] = now;
      void fetch(`${BASE}/api/threads/detail?key=${encodeURIComponent(threadKey)}`, {
        cache: "force-cache",
      }).catch(() => {});
    },
    [router],
  );

  const openThread = useCallback(
    (threadKey: string) => {
      const href = `/threads/${encodeURIComponent(threadKey)}`;
      router.push(href);
      onNavigate?.();
    },
    [onNavigate, router],
  );

  const focusThreadAt = useCallback(
    (nextIndex: number) => {
      const next = sortedThreads[nextIndex];
      if (!next) return;
      setFocusedThreadKey(next.slack_thread_key);
      const node = cardRefs.current[next.slack_thread_key];
      node?.focus();
      node?.scrollIntoView({ block: "nearest" });
    },
    [sortedThreads],
  );

  const handleListKeyDown = useCallback(
    (event: ReactKeyboardEvent<HTMLElement>) => {
      if (event.metaKey || event.ctrlKey || event.altKey) return;
      if (isTextInputTarget(event.target)) return;
      if (sortedThreads.length === 0) return;

      const currentIndex = Math.max(
        0,
        sortedThreads.findIndex((thread) => thread.slack_thread_key === focusedThreadKey),
      );
      if (event.key === "ArrowDown" || event.key.toLowerCase() === "j") {
        event.preventDefault();
        focusThreadAt(Math.min(currentIndex + 1, sortedThreads.length - 1));
        return;
      }
      if (event.key === "ArrowUp" || event.key.toLowerCase() === "k") {
        event.preventDefault();
        focusThreadAt(Math.max(currentIndex - 1, 0));
        return;
      }
      if (event.key === "Home") {
        event.preventDefault();
        focusThreadAt(0);
        return;
      }
      if (event.key === "End") {
        event.preventDefault();
        focusThreadAt(sortedThreads.length - 1);
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        const current = sortedThreads[currentIndex];
        if (current) openThread(current.slack_thread_key);
      }
    },
    [focusThreadAt, focusedThreadKey, openThread, sortedThreads],
  );

  useImperativeHandle(
    ref,
    () => ({
      focusSearch: () => {
        if (collapsed) {
          toggleRef.current?.focus();
          return;
        }
        searchRef.current?.focus();
      },
      focusSidebar: () => {
        if (collapsed) {
          toggleRef.current?.focus();
          return;
        }
        const selected =
          selectedThreadKey && cardRefs.current[selectedThreadKey]
            ? cardRefs.current[selectedThreadKey]
            : focusedThreadKey
              ? cardRefs.current[focusedThreadKey]
              : null;
        if (selected) {
          selected.focus();
          selected.scrollIntoView({ block: "nearest" });
          return;
        }
        searchRef.current?.focus();
      },
    }),
    [collapsed, focusedThreadKey, selectedThreadKey],
  );

  const canToggle = showCollapseToggle && Boolean(onCollapsedChange);

  if (collapsed) {
    return (
      <div className="flex h-full w-full flex-col items-center justify-end p-2">
        {canToggle ? (
          <button
            ref={toggleRef}
            type="button"
            onClick={() => onCollapsedChange?.(false)}
            aria-label="Expand sidebar"
            className="inline-flex size-8 items-center justify-center rounded-sm border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          >
            <ChevronRight className="size-4" />
          </button>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 w-full flex-col" onKeyDown={handleListKeyDown}>
      <div className="border-b border-border px-3 py-2">
        <div className="flex items-center justify-between gap-2">
          <div className="min-w-0">
            <h2 className="truncate text-sm font-semibold text-foreground">Threads</h2>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              {activeCount} active agent{activeCount === 1 ? "" : "s"}
            </p>
          </div>
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => void fetchThreads(true)}
              disabled={isRefreshing || !active}
              className="inline-flex items-center gap-1 rounded-sm border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground disabled:cursor-default disabled:opacity-60"
              aria-busy={isRefreshing}
            >
              <RefreshCw className={cn("size-3", isRefreshing ? "animate-spin" : "")} />
              {isRefreshing ? "..." : "Refresh"}
            </button>
            {canToggle ? (
              <button
                ref={toggleRef}
                type="button"
                onClick={() => onCollapsedChange?.(true)}
                aria-label="Collapse sidebar"
                className="inline-flex size-7 items-center justify-center rounded-sm border border-border text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
              >
                <ChevronLeft className="size-4" />
              </button>
            ) : null}
          </div>
        </div>
        <div className="mt-2">
          <label htmlFor={filterId} className="sr-only">
            Filter threads
          </label>
          <input
            ref={searchRef}
            id={filterId}
            name={filterId}
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="Filter threads… (/)"
            autoComplete="off"
            className="h-8 w-full rounded-sm border border-input bg-card px-2.5 text-xs text-foreground placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
          />
        </div>
        <div className="mt-2 inline-flex w-full rounded-sm border border-border bg-card p-0.5 text-[11px]">
          {([
            { id: "all", label: "All" },
            { id: "active", label: "Run" },
            { id: "idle", label: "Idle" },
            { id: "error", label: "Err" },
          ] as const).map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setStatusFilter(item.id)}
              className={cn(
                "flex-1 rounded-[2px] px-1.5 py-1 text-center text-muted-foreground transition-colors",
                statusFilter === item.id && "bg-accent text-foreground",
              )}
            >
              {item.label}
            </button>
          ))}
        </div>
      </div>

      <div className="flex-1 min-h-0 overflow-y-auto px-2.5 py-2" role="listbox" aria-label="Thread list">
        {loading ? (
          <div className="inline-flex w-full items-center justify-center gap-2 py-8 text-xs text-muted-foreground">
            <LoaderCircle className="size-3.5 animate-spin text-primary" />
            Loading threads...
          </div>
        ) : error && sortedThreads.length === 0 ? (
          <div className="space-y-2 py-8 text-center">
            <p className="text-xs text-destructive">{error}</p>
            <button
              type="button"
              onClick={() => void fetchThreads(true)}
              className="rounded-sm border border-border px-2 py-1 text-[11px] text-muted-foreground transition-colors hover:bg-accent hover:text-foreground"
            >
              Retry
            </button>
          </div>
        ) : sortedThreads.length === 0 ? (
          <div className="py-10 text-center text-xs text-muted-foreground">
            No threads match your filter.
          </div>
        ) : (
          <div className="space-y-2">
            {sortedThreads.map((thread) => {
              const name = thread.thread_name || threadName(thread.slack_thread_key);
              const href = `/threads/${encodeURIComponent(thread.slack_thread_key)}`;
              const rawTask =
                thread.last_user_message || thread.first_message || thread.last_result || "";
              const taskPreview = rawTask.replace(/^\[[\w]+\]\s*/, "").replace(/\s+/g, " ").slice(0, 120);
              const activeState = isRunningState(thread.state);
              const statusSubtitle = liveStatusByThread[thread.slack_thread_key] ?? runningSubtitle(thread);
              const activePhase = parseActivePhase(thread);
              const phaseIndex = activePhase
                ? PHASES.indexOf(activePhase as (typeof PHASES)[number])
                : -1;
              const progress = phaseIndex >= 0 ? ((phaseIndex + 1) / PHASES.length) * 100 : 0;
              const isSelected = selectedThreadKey === thread.slack_thread_key;
              const isFocused = focusedThreadKey === thread.slack_thread_key;

              return (
                <Link
                  key={thread.slack_thread_key}
                  ref={(node) => {
                    cardRefs.current[thread.slack_thread_key] = node;
                  }}
                  href={href}
                  prefetch={false}
                  role="option"
                  aria-selected={isSelected}
                  aria-current={isSelected ? "page" : undefined}
                  tabIndex={isFocused ? 0 : -1}
                  onMouseEnter={() => prefetchThread(thread.slack_thread_key)}
                  onFocus={() => {
                    setFocusedThreadKey(thread.slack_thread_key);
                    prefetchThread(thread.slack_thread_key);
                  }}
                  onClick={() => {
                    setFocusedThreadKey(thread.slack_thread_key);
                    onNavigate?.();
                  }}
                  className={cn(
                    "thread-sidebar-card block rounded-sm border border-border bg-card px-2.5 py-2 no-underline outline-none transition-colors hover:bg-accent/50 focus-visible:ring-1 focus-visible:ring-ring",
                    isSelected && "border-l-2 border-l-primary bg-accent",
                    activeState && "border-l-2 border-l-primary/70",
                  )}
                >
                  <div className="flex min-w-0 items-start justify-between gap-2">
                    <div className="min-w-0">
                      <div className="flex min-w-0 items-center gap-1.5">
                        <HarnessBadge harness={thread.harness} className="h-5 px-1.5 text-[9px]" />
                        <span className="truncate text-xs font-medium text-foreground">{name}</span>
                      </div>
                      <div className="mt-1 flex items-center gap-1 text-[11px] text-muted-foreground">
                        <span>
                          {thread.turn_count} turn{thread.turn_count === 1 ? "" : "s"}
                        </span>
                        <span>·</span>
                        <ThreadAge thread={thread} />
                        {thread.participants && thread.participants.length > 0 ? (
                          <>
                            <span>·</span>
                            <ParticipantAvatars participants={thread.participants} size={16} />
                          </>
                        ) : null}
                      </div>
                    </div>
                    <div className="inline-flex items-center gap-1 text-[10px] text-muted-foreground">
                      <StateDot state={thread.state} className="size-2.5" />
                      <span>{thread.state}</span>
                    </div>
                  </div>

                  {statusSubtitle ? (
                    <div className="mt-1 line-clamp-1 text-[11px] text-muted-foreground">{statusSubtitle}</div>
                  ) : null}
                  {taskPreview ? (
                    <div className="mt-1 line-clamp-1 text-[11px] leading-relaxed text-muted-foreground/90">
                      {taskPreview}
                    </div>
                  ) : null}
                  {activePhase ? <Progress value={progress} className="mt-2 h-0.5 bg-muted" /> : null}
                </Link>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
});
