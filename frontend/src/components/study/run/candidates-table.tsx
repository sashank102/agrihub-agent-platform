"use client";

import { Info } from "lucide-react";
import { Fragment } from "react";
import { Badge } from "@/components/ui/badge";
import type { CandidateRow, LocusRow, Tier } from "@/lib/run-events";
import type { StudyReport } from "@/lib/study-api";
import { formatBp, formatDistance } from "./format";

const CATEGORY_NAMES: Record<string, string> = {
  A: "positional/genetic",
  B: "same-species function",
  C: "ortholog-transferred",
  D: "annotation/pathway",
  E: "expression",
  F: "network",
  G: "GWAS/QTL convergence",
};

function CategoryPoints({ points }: { points?: Record<string, number> }) {
  const entries = Object.entries(points ?? {})
    .filter(([, value]) => value > 0)
    .sort(([left], [right]) => left.localeCompare(right));
  if (entries.length === 0) {
    return <span className="text-muted-foreground">—</span>;
  }
  return (
    <span
      className="flex flex-wrap gap-1"
      data-testid="category-points"
    >
      {entries.map(([code, value]) => (
        <span
          key={code}
          title={`${code}: ${CATEGORY_NAMES[code] ?? code}`}
          className="bg-muted rounded px-1 font-mono text-[11px] tabular-nums"
        >
          {code} {value.toFixed(1)}
        </span>
      ))}
    </span>
  );
}

const TIER_VARIANT: Record<Tier, "success" | "info" | "warning" | "outline"> = {
  T1: "success",
  T2: "info",
  T3: "warning",
  T4: "outline",
};

export function CandidatesTable({
  report,
  fallbackRows,
}: {
  report: StudyReport | null | undefined;
  fallbackRows: CandidateRow[] | null;
}) {
  const candidates = report?.candidates ?? fallbackRows ?? [];
  const loci: LocusRow[] = report?.loci ?? [];
  if (candidates.length === 0) {
    return (
      <p className="text-muted-foreground rounded-xl border border-dashed p-6 text-center text-sm">
        Ranked candidates appear when the ranking phase finishes.
      </p>
    );
  }
  const allPositional = candidates.every((item) => item.tier === "T4");
  const byLocus = new Map<string, CandidateRow[]>();
  for (const item of [...candidates].sort(
    (a, b) => (a.rank ?? 0) - (b.rank ?? 0),
  )) {
    byLocus.set(item.locus_id, [...(byLocus.get(item.locus_id) ?? []), item]);
  }
  const locusInfo = new Map(loci.map((locus) => [locus.locus_id, locus]));
  const order = loci.length
    ? loci.map((locus) => locus.locus_id).filter((id) => byLocus.has(id))
    : [...byLocus.keys()];

  return (
    <section
      aria-label="Candidate genes"
      className="flex flex-col gap-3"
      data-testid="candidates"
    >
      <div className="flex flex-wrap items-baseline gap-2">
        <h2 className="text-lg font-semibold tracking-tight">
          {report?.title ?? "Ranked candidates"}
        </h2>
        <span className="text-muted-foreground text-sm">
          {candidates.length} candidates across {order.length} loci
          {report ? ` · ${report.evidence_count} evidence items` : ""}
        </span>
      </div>
      {allPositional && (
        <p
          className="flex items-start gap-2 rounded-md border border-sky-200 bg-sky-50 p-3 text-sm text-sky-900 dark:border-sky-900 dark:bg-sky-950/40 dark:text-sky-100"
          data-testid="positional-only"
        >
          <Info className="mt-0.5 size-4 shrink-0" />
          <span>
            <span className="font-medium">Positional evidence only.</span> Every
            candidate is tier T4: the rubric found no curated or
            ortholog-transferred trait evidence, so the ranking rests on
            distance to the SNP and annotation relevance. Specialist findings
            are listed in the trace but do not change scores. Treat the order
            within a locus as a shortlist, not a call.
          </span>
        </p>
      )}
      <div className="bg-background overflow-x-auto rounded-xl border">
        <table className="w-full min-w-[980px] text-sm">
          <caption className="sr-only">
            Ranked candidate genes grouped by locus
          </caption>
          <thead className="bg-muted/60 text-muted-foreground text-left text-xs">
            <tr>
              <th className="px-3 py-2 font-medium">Rank</th>
              <th className="px-3 py-2 font-medium">Gene</th>
              <th className="px-3 py-2 font-medium">Position</th>
              <th className="px-3 py-2 font-medium">Distance</th>
              <th className="px-3 py-2 font-medium">Tier</th>
              <th className="px-3 py-2 text-right font-medium">Score</th>
              <th className="px-3 py-2 font-medium">Points (A–G)</th>
              <th className="px-3 py-2 font-medium">Description</th>
            </tr>
          </thead>
          <tbody>
            {order.map((locusId) => {
              const locus = locusInfo.get(locusId);
              return (
                <Fragment key={locusId}>
                  <tr className="bg-muted/30 border-t">
                    <th
                      colSpan={8}
                      scope="rowgroup"
                      className="px-3 py-1.5 text-left text-xs font-medium"
                    >
                      {locusId}
                      {locus && (
                        <span className="text-muted-foreground ml-2 font-normal">
                          {locus.chrom}:{formatBp(locus.start)}-
                          {formatBp(locus.end)} · lead {locus.lead_snp} ·{" "}
                          {locus.n_genes} genes
                        </span>
                      )}
                    </th>
                  </tr>
                  {(byLocus.get(locusId) ?? []).map((item) => (
                    <tr
                      key={item.gene_id}
                      className="border-t align-top"
                      data-gene={item.gene_id}
                    >
                      <td className="px-3 py-2 tabular-nums">
                        {item.rank}
                        {item.rank_in_locus ? (
                          <span className="text-muted-foreground text-xs">
                            {" "}
                            ({item.rank_in_locus} in locus)
                          </span>
                        ) : null}
                      </td>
                      <td className="px-3 py-2">
                        <span className="font-mono text-xs">
                          {item.gene_id}
                        </span>
                        {item.symbol && (
                          <span className="ml-1 font-medium">
                            {item.symbol}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2 font-mono text-xs whitespace-nowrap">
                        {item.chrom
                          ? `${item.chrom}:${formatBp(item.start)}-${formatBp(item.end)}`
                          : "—"}
                        {item.strand && item.strand !== "."
                          ? ` (${item.strand})`
                          : ""}
                      </td>
                      <td className="px-3 py-2 text-xs whitespace-nowrap">
                        {formatDistance(item.distance_bp, item.overlaps_snp)}
                        {item.nearest_snp && (
                          <span className="text-muted-foreground block">
                            {item.nearest_snp}
                          </span>
                        )}
                      </td>
                      <td className="px-3 py-2">
                        <Badge variant={TIER_VARIANT[item.tier] ?? "outline"}>
                          {item.tier}
                        </Badge>
                      </td>
                      <td className="px-3 py-2 text-right tabular-nums">
                        {item.score.toFixed(1)}
                      </td>
                      <td className="px-3 py-2 text-xs">
                        <CategoryPoints points={item.category_points} />
                      </td>
                      <td className="text-muted-foreground max-w-md px-3 py-2 text-xs">
                        {item.defline || "—"}
                      </td>
                    </tr>
                  ))}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      {report && report.limitations.length > 0 && (
        <details className="text-sm">
          <summary className="cursor-pointer font-medium">
            Limitations of this build
          </summary>
          <ul className="text-muted-foreground mt-2 list-disc pl-5">
            {report.limitations.map((item) => (
              <li key={item}>{item}</li>
            ))}
          </ul>
        </details>
      )}
    </section>
  );
}
