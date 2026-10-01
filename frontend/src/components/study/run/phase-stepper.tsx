"use client";

import {
  AlertTriangle,
  Check,
  Circle,
  CircleSlash,
  Loader2,
  Pause,
  X,
} from "lucide-react";
import { useShallow } from "zustand/react/shallow";
import {
  Collapsible,
  CollapsibleContent,
  CollapsibleTrigger,
} from "@/components/ui/collapsible";
import type { Phase } from "@/lib/run-events";
import { useRunStore, type PhaseState } from "@/lib/run-store";
import { cn } from "@/lib/utils";

const STEPS: { phase: Phase; label: string; optional?: boolean }[] = [
  { phase: "intake", label: "Intake" },
  { phase: "model", label: "Model", optional: true },
  { phase: "loci", label: "Loci" },
  { phase: "harvest", label: "Harvest" },
  { phase: "planning", label: "Plan" },
  { phase: "specialists", label: "Specialists" },
  { phase: "ranking", label: "Rank and verify" },
  { phase: "reporting", label: "Report" },
];

type StepState =
  "pending" | "running" | "done" | "failed" | "skipped" | "stopped";

function stepState(phase: PhaseState | undefined, stopped: boolean): StepState {
  if (!phase) {
    return "pending";
  }
  if (phase.status === "completed") {
    return "done";
  }
  if (phase.status === "failed") {
    return "failed";
  }
  if (phase.status === "skipped") {
    return "skipped";
  }
  return stopped ? "stopped" : "running";
}

function StepIcon({ state }: { state: StepState }) {
  const base = "size-4";
  switch (state) {
    case "done":
      return <Check className={cn(base, "text-emerald-600")} />;
    case "failed":
      return <X className={cn(base, "text-rose-600")} />;
    case "skipped":
      return <CircleSlash className={cn(base, "text-muted-foreground")} />;
    case "stopped":
      return <Pause className={cn(base, "text-amber-600")} />;
    case "running":
      return (
        <Loader2
          className={cn(
            base,
            "animate-spin text-sky-600 motion-reduce:animate-none",
          )}
        />
      );
    default:
      return <Circle className={cn(base, "text-muted-foreground/50")} />;
  }
}

const STATE_LABEL: Record<StepState, string> = {
  pending: "pending",
  running: "running",
  done: "completed",
  failed: "failed",
  skipped: "skipped",
  stopped: "stopped",
};

export function PhaseStepper() {
  const { phases, current, terminal } = useRunStore(
    useShallow((state) => ({
      phases: state.phases,
      current: state.phase,
      terminal: state.terminal,
    })),
  );
  const stopped = terminal !== null && terminal.status !== "completed";
  const steps = STEPS.filter((step) => !step.optional || phases[step.phase]);
  const failed = steps.find((step) => phases[step.phase]?.status === "failed");
  const failure = failed ? phases[failed.phase] : undefined;
  const warnings = Object.values(phases).flatMap((phase) => phase.warnings);
  const detail = current ? phases[current]?.detail : null;

  return (
    <section
      aria-label="Study phases"
      className="flex flex-col gap-3"
    >
      <ol className="bg-background flex flex-wrap items-center gap-x-1 gap-y-2 rounded-xl border px-3 py-2">
        {steps.map((step, index) => {
          const state = stepState(phases[step.phase], stopped);
          return (
            <li
              key={step.phase}
              className="flex items-center gap-1"
              aria-current={current === step.phase ? "step" : undefined}
              data-phase={step.phase}
              data-state={state}
            >
              <span
                className={cn(
                  "flex items-center gap-1.5 rounded-md px-2 py-1 text-sm",
                  current === step.phase && "bg-muted font-medium",
                  state === "pending" && "text-muted-foreground",
                  state === "failed" && "text-rose-700",
                )}
                title={phases[step.phase]?.detail ?? undefined}
              >
                <StepIcon state={state} />
                {step.label}
                <span className="sr-only">: {STATE_LABEL[state]}</span>
              </span>
              {index < steps.length - 1 && (
                <span
                  aria-hidden
                  className="bg-border h-px w-4"
                />
              )}
            </li>
          );
        })}
      </ol>
      {detail && !failure && (
        <p className="text-muted-foreground px-1 text-sm">{detail}</p>
      )}
      {failure && failed && (
        <div
          role="alert"
          className="flex flex-col gap-2 rounded-xl border border-rose-200 bg-rose-50 p-4 text-sm text-rose-900 dark:border-rose-900 dark:bg-rose-950/40 dark:text-rose-200"
        >
          <p className="font-medium">{failed.label} failed</p>
          {failure.detail && <p>{failure.detail}</p>}
          {failure.errors.length > 0 && (
            <ul className="list-disc pl-5">
              {failure.errors.map((error, index) => (
                <li key={index}>
                  {error.loc.length > 0 && (
                    <code className="mr-1 text-xs">{error.loc.join(".")}</code>
                  )}
                  {error.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {warnings.length > 0 && (
        <Collapsible className="px-1">
          <CollapsibleTrigger className="flex items-center gap-1.5 text-sm text-amber-700 hover:underline">
            <AlertTriangle className="size-4" />
            {warnings.length} input warning{warnings.length === 1 ? "" : "s"}
          </CollapsibleTrigger>
          <CollapsibleContent>
            <ul className="text-muted-foreground mt-2 list-disc pl-6 text-sm">
              {warnings.map((warning, index) => (
                <li key={index}>{warning.message}</li>
              ))}
            </ul>
          </CollapsibleContent>
        </Collapsible>
      )}
    </section>
  );
}
