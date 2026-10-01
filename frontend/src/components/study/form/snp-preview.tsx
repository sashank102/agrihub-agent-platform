"use client";

import { useVirtualizer } from "@tanstack/react-virtual";
import { AlertTriangle, Download, Loader2 } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Switch } from "@/components/ui/switch";
import {
  rejectedRowsCsv,
  summarize,
  type PreviewRow,
  type SnpRowStatus,
} from "@/lib/snp-parse";
import type { StudyValidation } from "@/lib/study-api";

const STATUS: Record<
  SnpRowStatus,
  { label: string; variant: "success" | "warning" | "destructive" | "info" }
> = {
  ok: { label: "ok", variant: "success" },
  warning: { label: "warning", variant: "warning" },
  invalid: { label: "invalid", variant: "destructive" },
  needs_lookup: { label: "needs lookup", variant: "info" },
};

const ROW_HEIGHT = 36;

function downloadCsv(name: string, text: string) {
  const url = URL.createObjectURL(new Blob([text], { type: "text/csv" }));
  const link = document.createElement("a");
  link.href = url;
  link.download = name;
  link.click();
  URL.revokeObjectURL(url);
}

export function SnpPreview({
  rows,
  validation,
  validating,
}: {
  rows: PreviewRow[];
  validation: StudyValidation | null;
  validating: boolean;
}) {
  const [problemsOnly, setProblemsOnly] = useState(false);
  const summary = useMemo(() => summarize(rows), [rows]);
  const visible = useMemo(
    () => (problemsOnly ? rows.filter((row) => row.status !== "ok") : rows),
    [rows, problemsOnly],
  );
  const scroller = useRef<HTMLDivElement>(null);
  const virtualizer = useVirtualizer({
    count: visible.length,
    getScrollElement: () => scroller.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: 8,
  });
  const rejected = summary.invalid + summary.duplicates;

  return (
    <section
      aria-label="SNP preview"
      className="flex flex-col gap-3"
    >
      <div className="flex flex-wrap items-center gap-3">
        <p
          className="text-sm font-medium"
          data-testid="snp-summary"
        >
          {summary.valid} valid · {summary.duplicates} duplicates ·{" "}
          {summary.invalid} invalid · {summary.needsLookup} need lookup
        </p>
        {validating ? (
          <span className="text-muted-foreground flex items-center gap-1 text-xs">
            <Loader2 className="size-3 animate-spin" />
            Checking with the server
          </span>
        ) : validation ? (
          <span className="text-muted-foreground text-xs">
            Checked by the server
          </span>
        ) : null}
        <div className="ml-auto flex items-center gap-3">
          <div className="flex items-center gap-2">
            <Switch
              id="problems-only"
              checked={problemsOnly}
              onCheckedChange={setProblemsOnly}
            />
            <Label
              htmlFor="problems-only"
              className="text-sm"
            >
              Problems only
            </Label>
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            disabled={rejected === 0}
            onClick={() =>
              downloadCsv("rejected-snps.csv", rejectedRowsCsv(rows))
            }
          >
            <Download />
            Rejected rows
          </Button>
        </div>
      </div>
      <div
        role="table"
        aria-label="Parsed SNPs"
        aria-rowcount={visible.length}
        className="bg-background overflow-hidden rounded-md border text-sm"
      >
        <div
          role="row"
          className="bg-muted/60 text-muted-foreground grid grid-cols-[3rem_minmax(8rem,1.2fr)_6rem_7rem_7rem_minmax(10rem,2fr)] gap-2 border-b px-3 py-2 text-xs font-medium"
        >
          <span role="columnheader">Line</span>
          <span role="columnheader">SNP</span>
          <span role="columnheader">Chrom</span>
          <span role="columnheader">Position</span>
          <span role="columnheader">Status</span>
          <span role="columnheader">Notes</span>
        </div>
        <div
          ref={scroller}
          className="max-h-72 overflow-auto"
        >
          {visible.length === 0 ? (
            <p className="text-muted-foreground px-3 py-6 text-center text-sm">
              {problemsOnly ? "No problems found." : "No SNPs yet."}
            </p>
          ) : (
            <div
              className="relative"
              style={{ height: virtualizer.getTotalSize() }}
            >
              {virtualizer.getVirtualItems().map((item) => {
                const row = visible[item.index];
                const status = STATUS[row.status];
                return (
                  <div
                    key={`${row.line}-${row.raw}-${item.index}`}
                    role="row"
                    data-status={row.status}
                    className="absolute inset-x-0 grid grid-cols-[3rem_minmax(8rem,1.2fr)_6rem_7rem_7rem_minmax(10rem,2fr)] items-center gap-2 border-b px-3"
                    style={{
                      height: ROW_HEIGHT,
                      transform: `translateY(${item.start}px)`,
                    }}
                  >
                    <span
                      role="cell"
                      className="text-muted-foreground tabular-nums"
                    >
                      {row.line}
                    </span>
                    <span
                      role="cell"
                      className="truncate font-mono text-xs"
                      title={row.raw}
                    >
                      {row.raw}
                    </span>
                    <span role="cell">{row.chrom ?? "—"}</span>
                    <span
                      role="cell"
                      className="tabular-nums"
                    >
                      {row.pos?.toLocaleString("en-US") ?? "—"}
                    </span>
                    <span role="cell">
                      <Badge variant={status.variant}>
                        {row.duplicateOf !== null ? "duplicate" : status.label}
                      </Badge>
                    </span>
                    <span
                      role="cell"
                      className="text-muted-foreground truncate text-xs"
                      title={row.issues
                        .map((issue) => issue.message)
                        .join("\n")}
                    >
                      {row.issues.map((issue) => issue.message).join("; ")}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

export function ServerPreview({
  validation,
  windowKb,
}: {
  validation: StudyValidation | null;
  windowKb: number;
}) {
  if (!validation) {
    return null;
  }
  const general = validation.warnings.filter((warning) => !warning.snp);
  const blocking = validation.errors.filter(
    (error) => !(error.loc[0] === "snps" && typeof error.loc[1] === "number"),
  );
  const { loci, genes } = validation.preview;
  return (
    <section
      aria-label="Server preview"
      className="bg-background flex flex-col gap-3 rounded-md border p-3"
      data-testid="server-preview"
    >
      <p className="text-sm font-medium">
        {loci.length} loci · {genes} candidate genes · ±{windowKb} kb windows
      </p>
      {blocking.map((error, index) => (
        <p
          key={index}
          role="alert"
          className="flex items-start gap-2 text-sm text-rose-600"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          {error.message}
        </p>
      ))}
      {general.map((warning, index) => (
        <p
          key={index}
          className="flex items-start gap-2 text-sm text-amber-700"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          {warning.message}
        </p>
      ))}
      {loci.length > 0 && (
        <ul className="grid gap-1 text-xs sm:grid-cols-2">
          {loci.map((locus) => (
            <li
              key={locus.locus_id}
              className="bg-muted/50 flex items-center gap-2 rounded px-2 py-1"
            >
              <span className="font-medium">{locus.locus_id}</span>
              <span className="font-mono">
                {locus.chrom}:{locus.start.toLocaleString("en-US")}-
                {locus.end.toLocaleString("en-US")}
              </span>
              <span className="text-muted-foreground ml-auto">
                {Object.keys(locus.snp_positions).length || 1} SNP ·{" "}
                {locus.n_genes} genes{locus.genes_capped ? " (capped)" : ""}
              </span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
