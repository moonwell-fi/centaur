"use client";

import { useEffect, useRef, useState } from "react";
import { Check, ChevronRight, CircleCheck, CircleX, LoaderCircle, X as XIcon } from "lucide-react";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { describeToolCall, type ToolCall } from "@/lib/describe";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { useIsMobile } from "@/hooks/use-media-query";
import { cn } from "@/lib/utils";

function ToolStateIcon({ state, hasOutput }: { state?: ToolCall["state"]; hasOutput?: boolean }) {
  if (state === "done") return <CircleCheck className="size-3.5 text-primary" />;
  if (state === "error") return <CircleX className="size-3.5 text-destructive" />;
  if (hasOutput) return <CircleCheck className="size-3.5 text-primary" />;
  return <LoaderCircle className="size-3.5 text-muted-foreground animate-spin" />;
}

function PillStatusIcon({ loading, error }: { loading: number; error: number }) {
  if (error > 0) return <XIcon className="size-4 text-destructive flex-shrink-0" />;
  if (loading > 0) return <LoaderCircle className="size-4 text-muted-foreground animate-spin flex-shrink-0" />;
  return <Check className="size-4 text-green-500 flex-shrink-0" />;
}

function ToolCallItem({ call, isMobile }: { call: ToolCall; isMobile: boolean }) {
  const [expandedOutput, setExpandedOutput] = useState(false);
  const output = call.output ?? "";
  const outputLines = output.split("\n");
  const showMobileToggle = isMobile && outputLines.length > 6;
  const previewOutput = showMobileToggle ? outputLines.slice(0, 6).join("\n") : output;

  return (
    <Collapsible className="group/call">
      <CollapsibleTrigger className="w-full flex items-center gap-2 py-1 text-xs text-muted-foreground hover:text-foreground cursor-pointer">
        <ChevronRight className="size-3 transition-transform group-data-[state=open]/call:rotate-90" />
        <ToolStateIcon state={call.state} hasOutput={!!call.output} />
        <span className="truncate">{describeToolCall(call.name, call.input)}</span>
        {call.output && (
          <Tooltip>
            <TooltipTrigger asChild>
              <span className="ml-auto tabular-nums text-[11px]">
                {call.output.length.toLocaleString()} chars
              </span>
            </TooltipTrigger>
            <TooltipContent>
              State: {call.state ?? "loading"} · Output: {call.output.length.toLocaleString()} chars
            </TooltipContent>
          </Tooltip>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent>
        {output ? (
          <div className="ml-5 space-y-1.5">
            {isMobile ? (
              <div className="rounded-sm bg-background p-2">
                <div className="relative">
                  <div className="whitespace-pre-wrap text-[11px] text-muted-foreground">
                    {expandedOutput ? output : previewOutput}
                  </div>
                  {showMobileToggle && !expandedOutput ? (
                    <div className="pointer-events-none absolute inset-x-0 bottom-0 h-8 bg-gradient-to-t from-background to-transparent" />
                  ) : null}
                </div>
                {showMobileToggle ? (
                  <button
                    type="button"
                    onClick={() => setExpandedOutput((value) => !value)}
                    className="mt-1 text-[11px] text-primary"
                  >
                    {expandedOutput ? "Collapse" : "Show full output"}
                  </button>
                ) : null}
              </div>
            ) : (
              <pre className="rounded-sm bg-background p-2 text-[11px] text-muted-foreground overflow-auto overscroll-contain max-h-[260px] whitespace-pre-wrap">
                {output}
              </pre>
            )}
          </div>
        ) : null}
      </CollapsibleContent>
    </Collapsible>
  );
}

export function StepGroup({
  icon: Icon,
  summary,
  calls,
}: {
  icon: React.ComponentType<{ className?: string }>;
  summary: string;
  calls: ToolCall[];
}) {
  const isMobile = useIsMobile();
  const loadingCount = calls.filter((call) => (call.state === "loading" || !call.state) && !call.output).length;
  const errorCount = calls.filter((call) => call.state === "error").length;
  const doneCount = calls.filter((call) => call.state === "done" || (call.output && call.state !== "error")).length;
  const manuallyToggled = useRef(false);
  const previousLoadingCount = useRef(loadingCount);
  const hasBeenActive = useRef(false);
  const [forceOpen, setForceOpen] = useState(!isMobile);

  useEffect(() => {
    if (loadingCount > 0) {
      hasBeenActive.current = true;
    }
  }, [loadingCount]);

  useEffect(() => {
    const wasLoading = previousLoadingCount.current > 0;
    previousLoadingCount.current = loadingCount;
    if (isMobile || manuallyToggled.current) return;
    if (loadingCount > 0 || errorCount > 0) {
      setForceOpen(true);
      return;
    }
    // Auto-collapse only after this group was actively loading and then completed.
    if (!wasLoading || !hasBeenActive.current) return;
    const timeout = window.setTimeout(() => setForceOpen(false), 2000);
    return () => window.clearTimeout(timeout);
  }, [errorCount, isMobile, loadingCount]);

  useEffect(() => {
    if (!isMobile && loadingCount > 0) {
      manuallyToggled.current = false;
    }
  }, [isMobile, loadingCount]);

  const isOpen = forceOpen;

  function handleToggle(nextOpen: boolean) {
    manuallyToggled.current = true;
    setForceOpen(nextOpen);
  }

  return (
    <Collapsible
      open={isOpen}
      onOpenChange={handleToggle}
      className={cn(
        "group step-item rounded-lg md:rounded-none",
        isMobile
          ? "bg-secondary/30 border border-border/30"
          : "border-0 border-l-2 border-l-border/70 bg-transparent pl-1",
      )}
    >
      <CollapsibleTrigger
        className={cn(
          "w-full flex items-center gap-2 px-3 py-2 cursor-pointer transition-colors",
          isMobile ? "min-h-[44px] active:bg-secondary/60" : "hover:bg-accent/50",
        )}
      >
        {isMobile ? (
          <PillStatusIcon loading={loadingCount} error={errorCount} />
        ) : (
          <>
            <ChevronRight className="size-3.5 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
            <Icon className="size-3.5 text-primary" />
          </>
        )}
        <span className={cn(
          "truncate flex-1 min-w-0 text-left",
          isMobile ? "text-sm text-muted-foreground" : "text-sm text-foreground",
        )}>
          {summary}
        </span>
        {!isMobile && (
          errorCount > 0 ? (
            <CircleX className="ml-auto size-3.5 text-destructive" />
          ) : loadingCount > 0 ? (
            <LoaderCircle className="ml-auto size-3.5 text-muted-foreground animate-spin" />
          ) : (
            <CircleCheck className="ml-auto size-3.5 text-primary" />
          )
        )}
        <span className="text-[10px] font-mono text-muted-foreground tabular-nums flex-shrink-0">
          {doneCount}/{calls.length}
        </span>
        {isMobile && (
          <ChevronRight className={cn(
            "size-4 text-muted-foreground/50 transition-transform flex-shrink-0",
            isOpen && "rotate-90",
          )} />
        )}
      </CollapsibleTrigger>
      <CollapsibleContent className="px-3 pb-2 pl-4 md:pl-6 space-y-1">
        {calls.map((call) => (
          <ToolCallItem key={call.id} call={call} isMobile={isMobile} />
        ))}
      </CollapsibleContent>
    </Collapsible>
  );
}
