"use client";

import {
  createColumnHelper,
  flexRender,
  getCoreRowModel,
  getFilteredRowModel,
  getPaginationRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnFiltersState,
} from "@tanstack/react-table";
import { useCallback, useEffect, useMemo, useState, Fragment } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { CandidateRow, LocusRow } from "@/lib/run-events";
import type { StudyReport } from "@/lib/study-api";
import { cn } from "@/lib/utils";
import { CandidatesTable } from "../run/candidates-table";
import { ResearchTrace } from "../run/research-trace";
import { CitationProvider, type CitationHandlers } from "./citation-text";
import {
  TIER_VARIANT,
  citationIndex,
  geneLinks,
  reportCandidates,
  sourceIndex,
} from "./exports";
import { SummaryView } from "./summary-view";

const CATEGORY_MAX: Record<string, number> = {
  A: 20,
  B: 25,
  C: 20,
  D: 10,
  E: 10,
  F: 10,
  G: 5,
};
const CATEGORIES = Object.keys(CATEGORY_MAX);

const TABS = [
  ["summary", "Summary"],
  ["candidates", "Candidates"],
  ["loci", "Loci"],
  ["matrix", "Evidence"],
  ["sources", "Sources"],
  ["methods", "Methods"],
  ["trace", "Trace"],
] as const;

type TabValue = (typeof TABS)[number][0];

const column = createColumnHelper<CandidateRow>();

function FullCandidates({
  rows,
  onOpen,
}: {
  rows: CandidateRow[];
  onOpen: (alias: string) => void;
}) {
  const [filter, setFilter] = useState("");
  const [columnFilters, setColumnFilters] = useState<ColumnFiltersState>([]);
  const [expanded, setExpanded] = useState<string | null>(null);
  const columns = useMemo(
    () => [
      column.accessor("rank", { header: "Rank" }),
      column.accessor("tier", { header: "Tier" }),
      column.accessor("score", { header: "Score" }),
      column.accessor("gene_id", { header: "Gene" }),
      column.accessor("symbol", { header: "Symbol" }),
      column.accessor("locus_id", { header: "Locus" }),
      column.accessor("distance_bp", { header: "Distance" }),
      column.accessor("verifier_status", { header: "Verifier" }),
    ],
    [],
  );
  const table = useReactTable({
    data: rows,
    columns,
    state: { columnFilters, globalFilter: filter },
    onColumnFiltersChange: setColumnFilters,
    onGlobalFilterChange: setFilter,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
    getFilteredRowModel: getFilteredRowModel(),
    getPaginationRowModel: getPaginationRowModel(),
    initialState: { pagination: { pageSize: 25 } },
  });
  return (
    <div
      className="space-y-3"
      data-testid="candidates-all"
    >
      <Input
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
        placeholder="Filter genes, loci, tiers"
        aria-label="Filter candidates"
        className="print:hidden"
      />
      <div className="overflow-x-auto rounded-xl border">
        <table className="w-full text-sm">
          <thead className="bg-muted/50 text-left">
            {table.getHeaderGroups().map((group) => (
              <tr key={group.id}>
                {group.headers.map((header) => (
                  <th
                    key={header.id}
                    className="px-2 py-2 font-medium"
                  >
                    {flexRender(
                      header.column.columnDef.header,
                      header.getContext(),
                    )}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {table.getRowModel().rows.map((row) => {
              const item = row.original;
              const open = expanded === item.gene_id;
              return (
                <Fragment key={item.gene_id}>
                  <tr
                    className="border-t"
                    data-gene-full={item.gene_id}
                  >
                    {row.getVisibleCells().map((cell) => (
                      <td
                        key={cell.id}
                        className="px-2 py-2 align-top"
                      >
                        {cell.column.id === "gene_id" ? (
                          <button
                            type="button"
                            className="font-mono"
                            onClick={() =>
                              setExpanded(open ? null : item.gene_id)
                            }
                          >
                            {item.gene_id}
                            {item.shortlist ? (
                              <Badge
                                className="ml-2"
                                variant="info"
                              >
                                top-K
                              </Badge>
                            ) : null}
                          </button>
                        ) : cell.column.id === "tier" ? (
                          <Badge variant={TIER_VARIANT[item.tier]}>
                            {item.tier}
                          </Badge>
                        ) : cell.column.id === "score" ? (
                          <CategoryBars points={item.category_points} />
                        ) : (
                          flexRender(
                            cell.column.columnDef.cell,
                            cell.getContext(),
                          )
                        )}
                      </td>
                    ))}
                  </tr>
                  {open ? (
                    <tr className="bg-muted/30 border-t">
                      <td
                        colSpan={row.getVisibleCells().length}
                        className="space-y-2 px-3 py-3 text-xs"
                      >
                        <EvidenceProfile
                          item={item}
                          onOpen={onOpen}
                        />
                      </td>
                    </tr>
                  ) : null}
                </Fragment>
              );
            })}
          </tbody>
        </table>
      </div>
      <div className="flex items-center gap-2 text-sm print:hidden">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => table.previousPage()}
          disabled={!table.getCanPreviousPage()}
        >
          Previous
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => table.nextPage()}
          disabled={!table.getCanNextPage()}
        >
          Next
        </Button>
        <span className="text-muted-foreground">
          Page {table.getState().pagination.pageIndex + 1} of{" "}
          {table.getPageCount() || 1}
        </span>
      </div>
    </div>
  );
}

function CategoryBars({ points }: { points?: Record<string, number> }) {
  return (
    <span className="flex items-end gap-0.5">
      <span className="mr-1 tabular-nums">
        {Object.values(points ?? {})
          .reduce((sum, value) => sum + value, 0)
          .toFixed(1)}
      </span>
      {CATEGORIES.map((code) => {
        const value = points?.[code] ?? 0;
        const height = Math.max(
          2,
          Math.round((value / CATEGORY_MAX[code]) * 16),
        );
        return (
          <span
            key={code}
            title={`${code} ${value}`}
            className="bg-primary/70 inline-block w-1.5 rounded-sm"
            style={{ height }}
          />
        );
      })}
    </span>
  );
}

function EvidenceProfile({
  item,
  onOpen,
}: {
  item: CandidateRow;
  onOpen: (alias: string) => void;
}) {
  const supporting = item.evidence_ids ?? [];
  const conflicting = item.conflicting_findings ?? [];
  const missing = CATEGORIES.filter(
    (code) => (item.category_points?.[code] ?? 0) === 0,
  );
  return (
    <div className="grid gap-2 sm:grid-cols-3">
      <p>
        <span className="font-medium">Supporting. </span>
        {supporting.length
          ? supporting.slice(0, 8).map((id) => (
              <button
                key={id}
                type="button"
                className="mr-1 font-mono underline"
                onClick={() => onOpen(id)}
              >
                {id}
              </button>
            ))
          : "none recorded"}
      </p>
      <p>
        <span className="font-medium">Conflicting. </span>
        {conflicting.length ? conflicting.join(", ") : "none"}
      </p>
      <p>
        <span className="font-medium">Missing. </span>
        {missing.join(", ") || "none"}
      </p>
    </div>
  );
}

export function ReportView({
  report,
  fallbackRows,
}: {
  report: StudyReport | null | undefined;
  fallbackRows: CandidateRow[] | null;
}) {
  const [tab, setTab] = useState<TabValue>("summary");
  const [focus, setFocus] = useState<string | null>(null);
  const [sourceFocus, setSourceFocus] = useState<string | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const citations = useMemo(
    () => (report ? citationIndex(report) : new Map()),
    [report],
  );
  const sources = useMemo(
    () => (report ? sourceIndex(report) : new Map()),
    [report],
  );
  const openSource = useCallback((sourceId: string) => {
    setSourceFocus(sourceId);
    setTab("sources");
  }, []);
  const handlers = useMemo<CitationHandlers>(
    () => ({
      citations,
      sources,
      onOpenEvidence: setFocus,
      onOpenSource: openSource,
    }),
    [citations, sources, openSource],
  );
  useEffect(() => {
    if (tab === "sources" && sourceFocus) {
      document
        .getElementById(`source-${sourceFocus}`)
        ?.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [tab, sourceFocus]);
  if (!report && !fallbackRows?.length) {
    return null;
  }
  const focused = focus ? citations.get(focus) : undefined;
  const rows = report ? reportCandidates(report) : (fallbackRows ?? []);
  return (
    <CitationProvider value={handlers}>
      <section
        className="report-print"
        data-testid="report-view"
      >
        <Tabs
          value={tab}
          onValueChange={(value) => setTab(value as TabValue)}
          className="gap-5"
        >
          <div className="bg-muted/30 sticky top-14 z-20 -mx-4 border-b px-4 py-2 backdrop-blur print:hidden">
            <TabsList className="flex h-auto flex-wrap">
              {TABS.map(([value, label]) => (
                <TabsTrigger
                  key={value}
                  value={value}
                >
                  {label}
                </TabsTrigger>
              ))}
            </TabsList>
          </div>
          {focused ? (
            <div
              className="bg-background flex items-start gap-3 rounded-md border p-3 text-sm print:hidden"
              data-testid="evidence-focus"
            >
              <p className="min-w-0 flex-1">
                <span className="font-mono font-medium">{focused.alias}</span> ·{" "}
                {focused.source_db} · {focused.category}:{" "}
                {focused.quote || focused.subtype}
              </p>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setFocus(null)}
              >
                Close
              </Button>
            </div>
          ) : null}
          <TabsContent
            value="summary"
            forceMount
            data-print-panel
            className="data-[state=inactive]:hidden"
          >
            {report ? (
              <SummaryView report={report} />
            ) : (
              <p className="text-muted-foreground text-sm">
                The written summary appears when the report is ready.
              </p>
            )}
          </TabsContent>
          <TabsContent
            value="candidates"
            forceMount
            data-print-panel
            className="space-y-6 data-[state=inactive]:hidden print:mt-8"
          >
            <CandidatesTable
              report={report}
              fallbackRows={fallbackRows}
            />
            {report ? (
              <FullCandidates
                rows={rows}
                onOpen={setFocus}
              />
            ) : null}
          </TabsContent>
          <TabsContent value="loci">
            <LociTab
              report={report}
              loci={report?.loci ?? []}
              rows={rows}
            />
          </TabsContent>
          <TabsContent value="matrix">
            <Matrix rows={rows.slice(0, 40)} />
          </TabsContent>
          <TabsContent value="sources">
            <ul className="space-y-2 text-sm">
              {(report?.sources ?? []).map((source) => (
                <li
                  key={source.source_id}
                  id={`source-${source.source_id}`}
                  className={cn(
                    "bg-background rounded-md border p-3 transition-colors",
                    sourceFocus === source.source_id &&
                      "border-primary ring-primary/30 ring-2",
                  )}
                >
                  <span className="font-medium">{source.name}</span>
                  <span className="text-muted-foreground">
                    {" "}
                    {source.version}
                    {source.retrieved_at
                      ? ` · retrieved ${source.retrieved_at}`
                      : ""}
                  </span>
                  <p>{source.license || "license not stated"}</p>
                  {source.url ? (
                    <a
                      href={source.url}
                      target="_blank"
                      rel="noreferrer"
                      className="text-primary text-xs break-all underline-offset-4 hover:underline"
                    >
                      {source.url}
                    </a>
                  ) : null}
                </li>
              ))}
            </ul>
          </TabsContent>
          <TabsContent
            value="methods"
            className="space-y-2 text-sm"
          >
            <p>
              {report?.species} {report?.assembly}, trait {report?.trait}, mode{" "}
              {report?.mode}.
            </p>
            <p>
              Rubric version {String(report?.provenance.rubric_version ?? "")}.
              Window sensitivity flanks{" "}
              {(report?.stability?.flanks_bp ?? []).join(", ") ||
                "50/100/250 kb"}
              .
            </p>
            <ul className="list-disc pl-5">
              {(report?.suggested_validations ?? []).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
            <ul className="text-muted-foreground list-disc pl-5">
              {(report?.limitations ?? []).map((item) => (
                <li key={item}>{item}</li>
              ))}
            </ul>
          </TabsContent>
          <TabsContent value="trace">
            <ResearchTrace
              open={traceOpen}
              onOpenChange={setTraceOpen}
            />
          </TabsContent>
        </Tabs>
      </section>
    </CitationProvider>
  );
}

function LociTab({
  report,
  loci,
  rows,
}: {
  report: StudyReport | null | undefined;
  loci: LocusRow[];
  rows: CandidateRow[];
}) {
  return (
    <div className="space-y-3">
      {loci.map((locus) => {
        const genes = rows
          .filter((row) => row.locus_id === locus.locus_id)
          .sort(
            (left, right) =>
              (left.rank_in_locus ?? Infinity) -
              (right.rank_in_locus ?? Infinity),
          );
        const lead = genes[0];
        const links = report && lead ? geneLinks(report, lead.gene_id) : [];
        return (
          <article
            key={locus.locus_id}
            className="rounded-xl border p-3 text-sm"
          >
            <h3 className="font-medium">
              {locus.locus_id} · {locus.chrom}:{locus.start}-{locus.end}
            </h3>
            <p className="text-muted-foreground">
              Lead {locus.lead_snp} · {locus.n_genes} genes ·{" "}
              {locus.window_method} window
              {locus.merged_from.length > 1
                ? ` · merged ${locus.merged_from.join(", ")}`
                : ""}
            </p>
            {lead ? (
              <p className="mt-2 flex flex-wrap items-center gap-2">
                <span>
                  Leader <span className="font-mono">{lead.gene_id}</span>
                  {lead.symbol ? ` (${lead.symbol})` : ""}
                </span>
                {links.map((link) => (
                  <a
                    key={link.name}
                    className="text-primary underline"
                    href={link.href}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {link.name}
                  </a>
                ))}
              </p>
            ) : null}
          </article>
        );
      })}
    </div>
  );
}

function Matrix({ rows }: { rows: CandidateRow[] }) {
  return (
    <div
      className="overflow-x-auto"
      data-testid="evidence-matrix"
    >
      <table className="text-xs">
        <thead>
          <tr>
            <th className="px-2 py-1 text-left">Gene</th>
            {CATEGORIES.map((code) => (
              <th
                key={code}
                className="px-2 py-1"
              >
                {code}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.gene_id}>
              <td className="px-2 py-1 font-mono">{row.gene_id}</td>
              {CATEGORIES.map((code) => {
                const value = row.category_points?.[code] ?? 0;
                const ratio = value / CATEGORY_MAX[code];
                return (
                  <td
                    key={code}
                    className="px-2 py-1 text-center"
                    style={{
                      background: `color-mix(in oklab, var(--primary) ${Math.round(ratio * 70)}%, transparent)`,
                    }}
                    title={`${code} ${value}`}
                  >
                    {value ? value.toFixed(0) : ""}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
