"use client";

import { motion } from "framer-motion";
import { ListTree } from "lucide-react";
import { useShallow } from "zustand/react/shallow";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from "@/components/ui/sheet";
import { harvestProgress, useRunStore } from "@/lib/run-store";
import { AgentLanes } from "./agent-lanes";
import { formatDuration } from "./format";
import { HarvestLane } from "./harvest-lane";
import { OrchestratorPanel } from "./orchestrator-panel";

function TraceRow({ laneId }: { laneId: string }) {
  const { lane, tools, evidence } = useRunStore(
    useShallow((state) => {
      const item = state.agents[laneId];
      const ids = new Set<string>();
      for (const id of item?.toolIds ?? []) {
        state.tools[id].evidenceIds.forEach((value) => ids.add(value));
      }
      return {
        lane: item,
        tools: item?.toolIds.length ?? 0,
        evidence: ids.size,
      };
    }),
  );
  if (!lane) {
    return null;
  }
  return (
    <motion.li
      layoutId={`lane-${lane.id}`}
      layoutDependency={0}
      className="bg-background flex items-center gap-3 rounded-md border px-3 py-1.5 text-xs"
      data-status={lane.status}
    >
      <span className="min-w-0 flex-1 truncate font-medium">{lane.name}</span>
      <Badge
        variant={
          lane.status === "completed"
            ? "success"
            : lane.status === "failed"
              ? "destructive"
              : "warning"
        }
      >
        {lane.status}
      </Badge>
      <span className="text-muted-foreground w-16 text-right tabular-nums">
        {formatDuration(lane.durationMs)}
      </span>
      <span className="text-muted-foreground w-16 text-right tabular-nums">
        {tools} tools
      </span>
      <span className="text-muted-foreground w-20 text-right tabular-nums">
        {lane.findings ?? 0} findings
      </span>
      <span className="text-muted-foreground hidden w-24 text-right tabular-nums sm:inline">
        {evidence} evidence
      </span>
    </motion.li>
  );
}

export function ResearchTrace({
  open,
  onOpenChange,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { order, harvest, phases, sources } = useRunStore(
    useShallow((state) => ({
      order: state.agentOrder,
      harvest: state.harvest,
      phases: state.phases,
      sources: Object.keys(state.sources).length,
    })),
  );
  const progress = harvestProgress({ harvest, phases });
  return (
    <section
      aria-label="Research trace"
      className="flex flex-col gap-2"
      data-testid="research-trace"
    >
      <div className="flex items-center gap-2">
        <h2 className="font-semibold tracking-tight">Research trace</h2>
        <span className="text-muted-foreground text-sm">
          {order.length} agents · harvest {Math.round(progress.ratio * 100)}% ·{" "}
          {sources} sources
        </span>
        <Button
          type="button"
          variant="outline"
          size="sm"
          className="ml-auto"
          onClick={() => onOpenChange(true)}
        >
          <ListTree />
          Open full trace
        </Button>
      </div>
      <ul className="grid gap-1.5 lg:grid-cols-2">
        {order.map((id) => (
          <TraceRow
            key={id}
            laneId={id}
          />
        ))}
      </ul>
      <Sheet
        open={open}
        onOpenChange={onOpenChange}
      >
        <SheetContent
          side="right"
          className="w-full overflow-y-auto sm:max-w-3xl"
        >
          <SheetHeader>
            <SheetTitle>Research trace</SheetTitle>
            <SheetDescription>
              Rebuilt from the run&apos;s recorded events: the
              orchestrator&apos;s plan and decisions, the harvest, and every
              specialist&apos;s tool calls.
            </SheetDescription>
          </SheetHeader>
          <div className="flex flex-col gap-6 px-4 pb-6">
            <OrchestratorPanel />
            <HarvestLane />
            <AgentLanes expandAll />
          </div>
        </SheetContent>
      </Sheet>
    </section>
  );
}
