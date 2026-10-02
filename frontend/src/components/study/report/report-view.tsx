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
import { Fragment, useMemo, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import { Input } from "@/components/ui/input";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import type { CandidateRow, LocusRow, Tier } from "@/lib/run-events";
import type { ReportCitation, StudyReport } from "@/lib/study-api";
import { useRunStore } from "@/lib/run-store";
import { CandidatesTable } from "../run/candidates-table";
import {
  candidatesCsv,
  citationIndex,
  evidenceCsv,
  reportCandidates,
  reportJson,
  reportMarkdown,
} from "./exports";

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

const SOY_LINKS = [
  {
    name: "Ensembl Plants",
    template: "https://plants.ensembl.org/Glycine_max/Gene/Summary?g={gene_id}",
  },
  {
    name: "SoyBase",
    template:
      "https://legacy.soybase.org/sbt/search/search_results.php?category=FeatureName&search_term={gene_id}",
  },
];

const TIER_VARIANT: Record<Tier, "success" | "info" | "warning" | "outline"> = {
  T1: "success",
  T2: "info",
  T3: "warning",
  T4: "outline",
};

function download(filename: string, contents: string, type: string) {
  const blob = new Blob([contents], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

function CitationText({
  markdown,
  citations,
  onOpen,
}: {
  markdown: string;
  citations: Map<string, ReportCitation>;
  onOpen: (alias: string) => void;
}) {
  const parts = markdown.split(/(\[E\d+\])/g);
  return (
    <div className="max-w-3xl space-y-3 text-sm leading-6 whitespace-pre-wrap">
      {parts.map((part, index) => {
        const match = /^\[(E\d+)\]$/.exec(part);
        if (!match?.[1]) {
          return <Fragment key={index}>{part}</Fragment>;
        }
        const alias = match[1];
        const citation = citations.get(alias);
        return (
          <HoverCard key={`${alias}-${index}`}>
            <HoverCardTrigger asChild>
              <button
                type="button"
                className="text-primary font-mono underline-offset-2 hover:underline"
                onClick={() => onOpen(alias)}
              >
                [{alias}]
              </button>
            </HoverCardTrigger>
            <HoverCardContent className="w-80 space-y-1 text-left text-xs">
              <p className="font-medium">
                {citation?.source_db ?? "Unknown source"}
              </p>
              <p className="text-muted-foreground">
                {citation
                  ? `${citation.category} · ${citation.subtype}`
                  : "This citation does not resolve."}
              </p>
              <p>{citation?.quote || "No verbatim quote was stored."}</p>
              <p className="text-muted-foreground">
                Verifier: {citation?.verifier_status ?? "unchecked"}
              </p>
            </HoverCardContent>
          </HoverCard>
        );
      })}
    </div>
  );
}

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
      <div className="flex items-center gap-2 text-sm">
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
  const [focus, setFocus] = useState<string | null>(null);
  if (!report && !fallbackRows?.length) {
    return null;
  }
  const citations = report ? citationIndex(report) : new Map();
  const focused = focus ? citations.get(focus) : undefined;
  const rows = report ? reportCandidates(report) : (fallbackRows ?? []);
  const slug = (report?.trait || "study").replaceAll(/\s+/g, "-");
  return (
    <section
      className="report-print space-y-4"
      data-testid="report-view"
    >
      <div className="flex flex-wrap gap-2 print:hidden">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() =>
            report &&
            download(`${slug}.json`, reportJson(report), "application/json")
          }
          disabled={!report}
        >
          JSON
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() =>
            report &&
            download(
              `${slug}-candidates.csv`,
              candidatesCsv(report),
              "text/csv",
            )
          }
          disabled={!report}
        >
          Candidates CSV
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() =>
            report &&
            download(`${slug}-evidence.csv`, evidenceCsv(report), "text/csv")
          }
          disabled={!report}
        >
          Evidence CSV
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() =>
            report &&
            download(`${slug}.md`, reportMarkdown(report), "text/markdown")
          }
          disabled={!report}
        >
          Markdown
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => window.print()}
        >
          Print PDF
        </Button>
      </div>
      <Tabs defaultValue="candidates">
        <TabsList className="flex h-auto flex-wrap print:hidden">
          {(
            [
              ["summary", "Summary"],
              ["candidates", "Candidates"],
              ["loci", "Loci"],
              ["matrix", "Evidence"],
              ["sources", "Sources"],
              ["methods", "Methods"],
              ["trace", "Trace"],
            ] as const
          ).map(([value, label]) => (
            <TabsTrigger
              key={value}
              value={value}
            >
              {label}
            </TabsTrigger>
          ))}
        </TabsList>
        <TabsContent value="summary">
          <CitationText
            markdown={report?.markdown || report?.title || ""}
            citations={citations}
            onOpen={setFocus}
          />
          {focused ? (
            <p
              className="mt-3 rounded-md border p-3 text-sm"
              data-testid="evidence-focus"
            >
              {focused.alias}: {focused.quote || focused.subtype} (
              {focused.source_db})
            </p>
          ) : null}
        </TabsContent>
        <TabsContent
          value="candidates"
          className="space-y-6"
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
                className="rounded-md border p-3"
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
            {(report?.stability?.flanks_bp ?? []).join(", ") || "50/100/250 kb"}
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
          <TraceList />
        </TabsContent>
      </Tabs>
    </section>
  );
}

function TraceList() {
  const order = useRunStore((state) => state.agentOrder);
  const agents = useRunStore((state) => state.agents);
  return (
    <ul className="space-y-1 text-sm">
      {order.map((id) => (
        <li key={id}>
          {agents[id]?.name} · {agents[id]?.status}
          {agents[id]?.summary ? ` · ${agents[id]?.summary}` : ""}
        </li>
      ))}
    </ul>
  );
}

function LociTab({ loci, rows }: { loci: LocusRow[]; rows: CandidateRow[] }) {
  return (
    <div className="space-y-3">
      {loci.map((locus) => {
        const genes = rows.filter((row) => row.locus_id === locus.locus_id);
        const lead = genes.find((row) => row.overlaps_snp) ?? genes[0];
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
              <p className="mt-2 flex flex-wrap gap-2">
                {SOY_LINKS.map((link) => (
                  <a
                    key={link.name}
                    className="text-primary underline"
                    href={link.template.replace("{gene_id}", lead.gene_id)}
                    target="_blank"
                    rel="noreferrer"
                  >
                    {link.name}: {lead.gene_id}
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
