"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { motion } from "framer-motion";
import {
  CheckCircle2,
  ChevronRight,
  Clock,
  Loader2,
  Wrench,
  XCircle,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useShallow } from "zustand/react/shallow";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import {
  useRunStore,
  type AgentState,
  type LaneStatus,
  type ToolState,
} from "@/lib/run-store";
import { cn } from "@/lib/utils";
import { between, formatDuration, formatTokens } from "./format";
import { useRunUi } from "./run-ui-store";

const LANE_BADGE: Record<
  LaneStatus,
  {
    label: string;
    variant: "info" | "success" | "destructive" | "warning" | "outline";
  }
> = {
  queued: { label: "Queued", variant: "outline" },
  running: { label: "Running", variant: "info" },
  completed: { label: "Completed", variant: "success" },
  failed: { label: "Failed", variant: "destructive" },
  cancelled: { label: "Cancelled", variant: "warning" },
};

function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [active]);
  return now;
}

function laneDuration(lane: AgentState, now: number): number | null {
  if (lane.durationMs !== null) {
    return lane.durationMs;
  }
  if (!lane.startedAt) {
    return null;
  }
  return lane.endedAt
    ? between(lane.startedAt, lane.endedAt)
    : now - Date.parse(lane.startedAt);
}

function ToolRow({ tool }: { tool: ToolState }) {
  const icon =
    tool.status === "running" ? (
      <Loader2 className="size-3.5 animate-spin text-sky-600 motion-reduce:animate-none" />
    ) : tool.status === "ok" ? (
      <CheckCircle2 className="size-3.5 text-emerald-600" />
    ) : (
      <XCircle className="size-3.5 text-rose-600" />
    );
  return (
    <Collapsible className="border-b last:border-b-0">
      <CollapsibleTrigger className="hover:bg-muted/50 group flex w-full items-center gap-2 px-2 py-1.5 text-left text-xs">
        <ChevronRight className="text-muted-foreground size-3 shrink-0 transition-transform group-data-[state=open]:rotate-90 motion-reduce:transition-none" />
        {icon}
        <span className="font-mono font-medium">{tool.name}</span>
        <span className="text-muted-foreground min-w-0 flex-1 truncate">
          {tool.inputSummary}
        </span>
        {tool.evidenceIds.length > 0 && (
          <span className="text-muted-foreground shrink-0 tabular-nums">
            {tool.evidenceIds.length} ev
          </span>
        )}
      </CollapsibleTrigger>
      <CollapsibleContent className="flex flex-col gap-1.5 px-7 pb-2 text-xs">
        {tool.inputSummary && (
          <p>
            <span className="text-muted-foreground">Input: </span>
            {tool.inputSummary}
          </p>
        )}
        {tool.outputSummary && (
          <p>
            <span className="text-muted-foreground">Result: </span>
            {tool.outputSummary}
          </p>
        )}
        {tool.evidenceIds.length > 0 && (
          <div className="flex flex-wrap gap-1">
            {tool.evidenceIds.slice(0, 24).map((id) => (
              <Badge
                key={id}
                variant="outline"
                className="font-mono"
              >
                {id}
              </Badge>
            ))}
            {tool.evidenceIds.length > 24 && (
              <Badge variant="outline">+{tool.evidenceIds.length - 24}</Badge>
            )}
          </div>
        )}
        {tool.outputRef && (
          <p className="text-muted-foreground font-mono">{tool.outputRef}</p>
        )}
      </CollapsibleContent>
    </Collapsible>
  );
}

function ToolLog({ laneId, name }: { laneId: string; name: string }) {
  const tools = useRunStore(
    useShallow((state) =>
      (state.agents[laneId]?.toolIds ?? []).map((id) => state.tools[id]),
    ),
  );
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: tools.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => 30,
    overscan: 6,
  });
  if (tools.length === 0) {
    return (
      <p className="text-muted-foreground px-2 py-3 text-xs">
        No tool calls yet.
      </p>
    );
  }
  return (
    <div
      ref={scroller}
      role="log"
      aria-label={`${name} tool calls`}
      aria-live="off"
      className="max-h-56 overflow-auto rounded-md border"
    >
      <div
        className="relative"
        style={{ height: virtualizer.getTotalSize() }}
      >
        {virtualizer.getVirtualItems().map((item) => (
          <div
            key={tools[item.index].id}
            ref={virtualizer.measureElement}
            data-index={item.index}
            className="absolute inset-x-0"
            style={{ transform: `translateY(${item.start}px)` }}
          >
            <ToolRow tool={tools[item.index]} />
          </div>
        ))}
      </div>
    </div>
  );
}

export function AgentLaneCard({
  laneId,
  defaultExpanded = false,
}: {
  laneId: string;
  defaultExpanded?: boolean;
}) {
  const lane = useRunStore((state) => state.agents[laneId]);
  const toolStats = useRunStore(
    useShallow((state) => {
      const ids = state.agents[laneId]?.toolIds ?? [];
      let errors = 0;
      const evidence = new Set<string>();
      for (const id of ids) {
        const tool = state.tools[id];
        if (tool.status === "error") errors += 1;
        tool.evidenceIds.forEach((item) => evidence.add(item));
      }
      return { count: ids.length, errors, evidence: evidence.size };
    }),
  );
  const highlighted = useRunUi((state) => state.highlighted.includes(laneId));
  const showDispatch = useRunUi((state) => state.showDispatch);
  const [expanded, setExpanded] = useState(defaultExpanded);
  const running = lane?.status === "running";
  const now = useNow(running);
  if (!lane) {
    return null;
  }
  const badge = LANE_BADGE[lane.status];
  const compact =
    !expanded && (lane.status === "completed" || lane.status === "cancelled");
  const tokens = lane.inputTokens + lane.outputTokens;

  return (
    <motion.article
      layoutId={defaultExpanded ? undefined : `lane-${lane.id}`}
      layoutDependency={0}
      id={defaultExpanded ? undefined : `lane-${lane.id}`}
      aria-label={lane.name}
      data-status={lane.status}
      data-highlighted={highlighted || undefined}
      className={cn(
        "bg-background flex flex-col gap-3 rounded-xl border p-3 shadow-xs transition-shadow motion-reduce:transition-none",
        highlighted && "ring-2 ring-sky-400",
        lane.status === "failed" && "border-rose-300",
      )}
    >
      <header className="flex items-start gap-2">
        <button
          type="button"
          className="focus-visible:ring-ring/50 min-w-0 flex-1 rounded-sm text-left outline-none focus-visible:ring-[3px]"
          onClick={() =>
            lane.dispatchEventId && showDispatch(lane.dispatchEventId)
          }
          title="Show the decision that dispatched this agent"
        >
          <h3 className="truncate text-sm font-semibold">{lane.name}</h3>
          <p className="text-muted-foreground truncate text-xs">
            {lane.label ?? lane.specialist ?? lane.kind}
          </p>
        </button>
        <Badge variant={badge.variant}>{badge.label}</Badge>
      </header>
      <div className="text-muted-foreground flex flex-wrap items-center gap-x-3 gap-y-1 text-xs tabular-nums">
        <span>
          Step {lane.step}/{lane.maxSteps ?? "?"}
        </span>
        <span className="flex items-center gap-1">
          <Clock className="size-3" />
          {formatDuration(laneDuration(lane, now))}
        </span>
        <span title={lane.model ? `model ${lane.model}` : undefined}>
          {formatTokens(tokens)} tokens{lane.model === "stub" ? " (stub)" : ""}
        </span>
        <span className="flex items-center gap-1">
          <Wrench className="size-3" />
          {toolStats.count} tools
          {toolStats.errors > 0 && (
            <span className="text-rose-600"> · {toolStats.errors} errors</span>
          )}
        </span>
        <span>
          {lane.findings ?? 0} findings · {toolStats.evidence} evidence
        </span>
      </div>
      {compact ? (
        <div className="flex items-center gap-2">
          <p className="text-muted-foreground min-w-0 flex-1 truncate text-xs">
            {lane.summary}
          </p>
          <Button
            type="button"
            variant="ghost"
            size="sm"
            onClick={() => setExpanded(true)}
          >
            Details
          </Button>
        </div>
      ) : (
        <>
          {lane.stepTitle && (
            <p
              className={cn(
                "text-sm",
                running && "shimmer-text motion-reduce:animate-none",
              )}
            >
              {lane.stepTitle}
            </p>
          )}
          {lane.summary && lane.status !== "running" && (
            <p className="text-muted-foreground text-xs">{lane.summary}</p>
          )}
          <ToolLog
            laneId={lane.id}
            name={lane.name}
          />
          {expanded && !defaultExpanded && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="self-end"
              onClick={() => setExpanded(false)}
            >
              Collapse
            </Button>
          )}
        </>
      )}
    </motion.article>
  );
}

export function AgentLanes({ expandAll = false }: { expandAll?: boolean }) {
  const order = useRunStore((state) => state.agentOrder);
  if (order.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-center text-sm">
        Specialist lanes open when the orchestrator dispatches them.
      </p>
    );
  }
  return (
    <div
      className="grid gap-3 md:grid-cols-2 2xl:grid-cols-3"
      data-testid="agent-lanes"
    >
      {order.map((id) => (
        <AgentLaneCard
          key={id}
          laneId={id}
          defaultExpanded={expandAll}
        />
      ))}
    </div>
  );
}
