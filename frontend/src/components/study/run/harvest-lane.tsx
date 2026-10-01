"use client";

import { Database } from "lucide-react";
import { useShallow } from "zustand/react/shallow";
import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { harvestProgress, useRunStore } from "@/lib/run-store";

const LABELS: Record<string, string> = {
  known_genes: "Known trait genes",
  annotation_relevance: "Annotation relevance",
  functional_annotation: "Functional annotation",
  orthologs: "Orthologs",
  arabidopsis: "Arabidopsis",
  qtl: "QTL overlap",
  gwas_catalog: "GWAS catalog",
};

export function HarvestLane() {
  const { harvest, phase } = useRunStore(
    useShallow((state) => ({
      harvest: state.harvest,
      phase: state.phases.harvest,
    })),
  );
  const categories = Object.values(harvest);
  const overall = harvestProgress({ harvest, phases: { harvest: phase } });
  if (!phase && categories.length === 0) {
    return null;
  }
  const status =
    phase?.status === "completed"
      ? { label: "Completed", variant: "success" as const }
      : phase?.status === "failed"
        ? { label: "Failed", variant: "destructive" as const }
        : { label: "Running", variant: "info" as const };
  return (
    <article
      aria-label="Harvest"
      className="bg-background flex flex-col gap-3 rounded-xl border p-3 shadow-xs"
      data-testid="harvest-lane"
    >
      <header className="flex items-center gap-2">
        <Database className="text-muted-foreground size-4" />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-semibold">Harvest</h3>
          <p className="text-muted-foreground text-xs">
            Deterministic evidence collection for every candidate gene
          </p>
        </div>
        <span
          className="text-sm font-medium tabular-nums"
          data-testid="harvest-percent"
        >
          {Math.round(overall.ratio * 100)}%
        </span>
        <Badge variant={status.variant}>{status.label}</Badge>
      </header>
      <ul className="grid gap-2 sm:grid-cols-2">
        {categories.map((item) => {
          const percent = item.total > 0 ? (item.done / item.total) * 100 : 100;
          return (
            <li
              key={item.category}
              className="flex flex-col gap-1"
            >
              <div className="flex justify-between text-xs">
                <span>{LABELS[item.category] ?? item.category}</span>
                <span className="text-muted-foreground tabular-nums">
                  {item.done}/{item.total}
                </span>
              </div>
              <Progress
                value={percent}
                aria-label={`${LABELS[item.category] ?? item.category} progress`}
                indicatorClassName={
                  percent >= 100 ? "bg-emerald-600" : "bg-sky-600"
                }
              />
            </li>
          );
        })}
      </ul>
    </article>
  );
}
