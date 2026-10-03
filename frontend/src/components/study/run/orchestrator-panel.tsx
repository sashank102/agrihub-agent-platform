"use client";

import {
  CheckCircle2,
  Circle,
  GitBranch,
  Loader2,
  PauseCircle,
  XCircle,
} from "lucide-react";
import { useEffect, useMemo, useRef } from "react";
import { useShallow } from "zustand/react/shallow";
import { Badge } from "@/components/ui/badge";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import type { LocusRow } from "@/lib/run-events";
import {
  useRunStore,
  type AgentState,
  type DecisionState,
  type RunState,
} from "@/lib/run-store";
import { cn } from "@/lib/utils";
import { useRunUi } from "./run-ui-store";

type StepStatus = "done" | "running" | "failed" | "stopped" | "pending";

function planStepStatus(step: string, state: RunState): StepStatus {
  const prefix = step.split(":")[0].trim();
  const lane = Object.values(state.agents).find((item) => item.name === prefix);
  if (lane) {
    if (lane.status === "completed") return "done";
    if (lane.status === "failed") return "failed";
    if (lane.status === "cancelled") return "stopped";
    return lane.status === "running" ? "running" : "pending";
  }
  const phase = step.startsWith("Collect")
    ? state.phases.specialists
    : step.startsWith("Rank")
      ? state.phases.ranking
      : step.startsWith("Write")
        ? state.phases.reporting
        : undefined;
  if (!phase) return "pending";
  if (phase.status === "completed") return "done";
  if (phase.status === "failed") return "failed";
  if (!state.terminal) return "running";
  return state.terminal.status === "failed" ? "failed" : "stopped";
}

function StatusIcon({ status }: { status: StepStatus }) {
  if (status === "done") {
    return <CheckCircle2 className="size-4 shrink-0 text-emerald-600" />;
  }
  if (status === "failed") {
    return <XCircle className="size-4 shrink-0 text-rose-600" />;
  }
  if (status === "stopped") {
    return <PauseCircle className="size-4 shrink-0 text-amber-600" />;
  }
  if (status === "running") {
    return (
      <Loader2 className="size-4 shrink-0 animate-spin text-sky-600 motion-reduce:animate-none" />
    );
  }
  return <Circle className="text-muted-foreground/50 size-4 shrink-0" />;
}

const NO_LOCI: LocusRow[] = [];

function TriageSummary() {
  const { loci, evidence, sources, harvestDetail } = useRunStore(
    useShallow((state) => ({
      loci: (state.artifacts.loci_table?.rows as LocusRow[] | null) ?? NO_LOCI,
      evidence: state.evidenceCounts,
      sources: Object.keys(state.sources).length,
      harvestDetail: state.phases.harvest?.detail ?? null,
    })),
  );
  const genes = loci.reduce((sum, locus) => sum + (locus.n_genes ?? 0), 0);
  const items = Object.values(evidence).reduce((sum, count) => sum + count, 0);
  const categories = Object.keys(evidence).length;
  return (
    <section aria-label="Triage summary">
      <h3 className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
        Triage
      </h3>
      <dl className="grid grid-cols-3 gap-2 text-center">
        {[
          ["Loci", loci.length],
          ["Genes", genes],
          ["Evidence", items],
        ].map(([label, value]) => (
          <div
            key={label}
            className="bg-muted/50 rounded-md px-2 py-1.5"
          >
            <dt className="text-muted-foreground text-xs">{label}</dt>
            <dd className="text-lg font-semibold tabular-nums">{value}</dd>
          </div>
        ))}
      </dl>
      <p className="text-muted-foreground mt-2 text-xs">
        {categories} evidence categories from {sources} sources
        {harvestDetail ? `; ${harvestDetail}` : ""}
      </p>
    </section>
  );
}

function PlanChecklist() {
  const state = useRunStore((store) => store);
  const plan = state.plan;
  if (!plan) {
    return (
      <p className="text-muted-foreground text-sm">
        The orchestrator plans once the harvest is done.
      </p>
    );
  }
  return (
    <section aria-label="Research plan">
      <h3 className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
        Plan
      </h3>
      <p className="mb-2 text-sm">{plan.summary}</p>
      <ol className="flex flex-col gap-1.5">
        {plan.steps.map((step) => {
          const status = planStepStatus(step, state);
          return (
            <li
              key={step}
              className="flex items-start gap-2 text-sm"
              data-status={status}
            >
              <StatusIcon status={status} />
              <span
                className={cn(status === "done" && "text-muted-foreground")}
              >
                {step}
              </span>
            </li>
          );
        })}
      </ol>
    </section>
  );
}

function DispatchChip({
  lane,
  agentId,
}: {
  lane?: AgentState;
  agentId: string;
}) {
  const focus = lane?.focus;
  return (
    <HoverCard openDelay={150}>
      <HoverCardTrigger asChild>
        <button
          type="button"
          className="focus-visible:ring-ring/50 rounded-md outline-none focus-visible:ring-[3px]"
          onClick={() =>
            document
              .getElementById(`lane-${agentId}`)
              ?.scrollIntoView({ block: "nearest", behavior: "smooth" })
          }
        >
          <Badge variant="secondary">{lane?.name ?? agentId}</Badge>
        </button>
      </HoverCardTrigger>
      <HoverCardContent className="w-80 text-xs">
        <p className="mb-1 font-medium">{lane?.name ?? agentId}</p>
        {lane?.label && (
          <p className="text-muted-foreground mb-2">{lane.label}</p>
        )}
        {focus?.instructions && <p className="mb-2">{focus.instructions}</p>}
        {focus?.rationale && (
          <p className="text-muted-foreground mb-2">Why: {focus.rationale}</p>
        )}
        {focus?.gene_ids && focus.gene_ids.length > 0 && (
          <p className="font-mono break-words">
            {focus.gene_ids.slice(0, 12).join(", ")}
            {focus.gene_ids.length > 12
              ? ` +${focus.gene_ids.length - 12}`
              : ""}
          </p>
        )}
      </HoverCardContent>
    </HoverCard>
  );
}

function DecisionItem({ decision }: { decision: DecisionState }) {
  const agents = useRunStore((state) => state.agents);
  const { highlight, clearHighlight, scrollTarget } = useRunUi(
    useShallow((state) => ({
      highlight: state.highlight,
      clearHighlight: state.clearHighlight,
      scrollTarget: state.scrollTarget,
    })),
  );
  const ref = useRef<HTMLLIElement>(null);
  const targeted = scrollTarget?.eventId === decision.eventId;
  const ids = useMemo(
    () => decision.dispatched.map((item) => item.agent_id),
    [decision.dispatched],
  );

  useEffect(() => {
    if (targeted) {
      ref.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      ref.current?.focus({ preventScroll: true });
    }
  }, [targeted, scrollTarget?.nonce]);

  return (
    <li
      ref={ref}
      id={`decision-${decision.eventId}`}
      tabIndex={-1}
      data-kind={decision.kind}
      onMouseEnter={() => highlight(ids)}
      onMouseLeave={clearHighlight}
      onFocus={() => highlight(ids)}
      onBlur={clearHighlight}
      className={cn(
        "relative flex flex-col gap-2 rounded-md border p-3 transition-shadow outline-none motion-reduce:transition-none",
        targeted && "ring-2 ring-sky-400",
      )}
    >
      <div className="flex items-center gap-2">
        <GitBranch className="text-muted-foreground size-4" />
        <Badge
          variant={
            decision.kind === "dispatch" || decision.kind === "followup"
              ? "info"
              : "outline"
          }
        >
          {decision.kind === "followup"
            ? "follow-up"
            : decision.kind === "select_model"
              ? "model choice"
              : decision.kind}
        </Badge>
        {decision.round > 1 && (
          <span className="text-muted-foreground text-xs">
            round {decision.round}
          </span>
        )}
        <time
          className="text-muted-foreground ml-auto text-xs tabular-nums"
          dateTime={decision.ts}
        >
          {new Date(decision.ts).toLocaleTimeString()}
        </time>
      </div>
      <p className="text-sm">{decision.rationale}</p>
      {decision.dispatched.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {decision.dispatched.map((item) => (
            <li
              key={item.agent_id}
              className="flex flex-col items-start gap-0.5"
            >
              <span className="flex items-center gap-1.5">
                <DispatchChip
                  agentId={item.agent_id}
                  lane={agents[item.agent_id]}
                />
                <span className="text-muted-foreground text-xs tabular-nums">
                  {item.focus_gene_ids.length} genes
                  {item.focus_loci.length > 0
                    ? `, ${item.focus_loci.join(" ")}`
                    : ""}
                </span>
              </span>
              {item.rationale && (
                <p className="text-muted-foreground text-xs">
                  {item.rationale}
                </p>
              )}
            </li>
          ))}
        </ul>
      )}
      {decision.selected && decision.selected.length > 0 && (
        <ul
          className="flex flex-col gap-1 text-xs"
          aria-label="Chosen models"
        >
          {decision.selected.map((item) => (
            <li
              key={`${item.model_id}:${item.dataset ?? ""}`}
              className="flex flex-wrap items-center gap-1.5"
            >
              <span className="font-medium">{item.name}</span>
              {item.dataset && <Badge variant="info">{item.dataset}</Badge>}
              <Badge variant="outline">{item.score_type}</Badge>
              <span className="text-muted-foreground">{item.label}</span>
            </li>
          ))}
        </ul>
      )}
      {decision.rejected.length > 0 && (
        <ul className="text-muted-foreground text-xs">
          {decision.rejected.map((item) => (
            <li key={item.specialist}>
              {decision.kind === "select_model"
                ? "Not applicable"
                : "Not dispatched"}
              : {item.specialist.replace(/_/g, " ")} ({item.reason})
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

export function OrchestratorPanel() {
  const decisions = useRunStore((state) => state.decisions);
  return (
    <aside
      aria-label="Orchestrator"
      className="flex flex-col gap-5"
    >
      <h2 className="font-semibold tracking-tight">Orchestrator</h2>
      <TriageSummary />
      <PlanChecklist />
      <section aria-label="Decisions">
        <h3 className="text-muted-foreground mb-2 text-xs font-medium tracking-wide uppercase">
          Decisions
        </h3>
        {decisions.length === 0 ? (
          <p className="text-muted-foreground text-sm">No decisions yet.</p>
        ) : (
          <ol className="flex flex-col gap-2">
            {decisions.map((decision) => (
              <DecisionItem
                key={decision.eventId}
                decision={decision}
              />
            ))}
          </ol>
        )}
      </section>
    </aside>
  );
}
