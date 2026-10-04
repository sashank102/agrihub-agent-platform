"use client";

import { ExternalLink } from "lucide-react";
import type { ReactNode } from "react";
import { Badge } from "@/components/ui/badge";
import type { ReportSummary, StudyReport } from "@/lib/study-api";
import { CitationText } from "./citation-text";
import { TIER_VARIANT, geneLinks, reportCandidates } from "./exports";

function Section({
  id,
  title,
  children,
}: {
  id: string;
  title: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-labelledby={id}
      className="space-y-3"
    >
      <h3
        id={id}
        className="text-base font-semibold tracking-tight"
      >
        {title}
      </h3>
      {children}
    </section>
  );
}

function Candidate({
  report,
  item,
}: {
  report: StudyReport;
  item: ReportSummary["candidates"][number];
}) {
  const links = geneLinks(report, item.gene_id);
  const [primary, ...others] = links;
  return (
    <article
      className="bg-background space-y-2 rounded-xl border p-4"
      data-summary-gene={item.gene_id}
    >
      <header className="flex flex-wrap items-center gap-2">
        <h4 className="font-mono text-sm font-semibold">
          {primary ? (
            <a
              href={primary.href}
              target="_blank"
              rel="noreferrer"
              className="text-primary underline-offset-4 hover:underline"
              title={`Open in ${primary.name}`}
            >
              {item.gene_id}
            </a>
          ) : (
            item.gene_id
          )}
        </h4>
        {item.symbol ? (
          <span className="text-sm font-medium">{item.symbol}</span>
        ) : null}
        {item.tier ? (
          <Badge variant={TIER_VARIANT[item.tier]}>{item.tier}</Badge>
        ) : null}
        {item.confidence ? (
          <span className="text-muted-foreground text-xs">
            {item.confidence} confidence
          </span>
        ) : null}
        {item.locus_id ? (
          <span className="text-muted-foreground text-xs">
            · {item.locus_id}
          </span>
        ) : null}
        {others.length ? (
          <span className="ml-auto flex flex-wrap gap-3 text-xs print:hidden">
            {links.map((link) => (
              <a
                key={link.name}
                href={link.href}
                target="_blank"
                rel="noreferrer"
                className="text-muted-foreground hover:text-primary inline-flex items-center gap-1"
              >
                {link.name}
                <ExternalLink className="size-3" />
              </a>
            ))}
          </span>
        ) : null}
      </header>
      <CitationText markdown={item.narrative} />
    </article>
  );
}

function AtAGlance({
  report,
  summary,
}: {
  report: StudyReport;
  summary: ReportSummary;
}) {
  const rows = new Map(
    reportCandidates(report).map((row) => [row.gene_id, row]),
  );
  return (
    <aside
      aria-label="Candidates at a glance"
      className="bg-background h-fit space-y-3 rounded-xl border p-4 text-sm xl:sticky xl:top-28"
    >
      <h3 className="font-semibold tracking-tight">Candidates at a glance</h3>
      <ol className="space-y-2">
        {summary.candidates.map((item, index) => {
          const row = rows.get(item.gene_id);
          return (
            <li
              key={item.gene_id}
              className="flex items-center gap-2"
            >
              <span className="text-muted-foreground w-4 tabular-nums">
                {index + 1}
              </span>
              <span className="min-w-0 flex-1 truncate font-mono text-xs">
                {item.gene_id}
                {item.symbol ? (
                  <span className="font-sans"> ({item.symbol})</span>
                ) : null}
              </span>
              {item.tier ? (
                <Badge variant={TIER_VARIANT[item.tier]}>{item.tier}</Badge>
              ) : null}
              {typeof row?.share_of_locus === "number" ? (
                <span
                  className="text-muted-foreground w-10 text-right text-xs tabular-nums"
                  title="Share of its locus score"
                >
                  {Math.round(row.share_of_locus * 100)}%
                </span>
              ) : null}
            </li>
          );
        })}
      </ol>
      <dl className="text-muted-foreground grid grid-cols-2 gap-x-3 gap-y-1 border-t pt-3 text-xs">
        <dt>Loci</dt>
        <dd className="text-right tabular-nums">{report.loci.length}</dd>
        <dt>Genes scored</dt>
        <dd className="text-right tabular-nums">{rows.size}</dd>
        <dt>Evidence items</dt>
        <dd className="text-right tabular-nums">{report.evidence_count}</dd>
        <dt>Written by</dt>
        <dd className="truncate text-right">
          {summary.written_by === "template"
            ? "deterministic template"
            : summary.written_by}
        </dd>
      </dl>
    </aside>
  );
}

/** The executive summary: bottom line first, then findings, candidates, loci, caveats and next steps. */
export function SummaryView({ report }: { report: StudyReport }) {
  const summary = report.summary;
  if (!summary) {
    return (
      <CitationText
        markdown={report.markdown || report.title}
        className="max-w-[80ch]"
      />
    );
  }
  return (
    <div
      className="grid gap-8 xl:grid-cols-[minmax(0,80ch)_minmax(16rem,22rem)]"
      data-testid="summary-view"
    >
      <article className="min-w-0 space-y-8">
        <header className="space-y-1">
          <h2 className="text-2xl font-semibold tracking-tight first-letter:uppercase">
            {report.title}
          </h2>
          <p className="text-muted-foreground text-sm">
            <span className="capitalize">{report.species}</span>, assembly{" "}
            {report.assembly}, trait {report.trait}. Confidence follows the
            deterministic tier: T1 strong, T2 moderate, T3 suggestive, T4
            positional only.
          </p>
        </header>
        <section
          aria-labelledby="summary-bottom-line"
          className="border-primary bg-primary/5 space-y-2 rounded-r-xl border-l-4 p-5"
          data-testid="summary-bottom-line"
        >
          <h3
            id="summary-bottom-line"
            className="text-muted-foreground text-xs font-semibold tracking-wide uppercase"
          >
            Bottom line
          </h3>
          <CitationText
            markdown={summary.bottom_line}
            className="text-base"
          />
        </section>
        {summary.key_findings.length ? (
          <Section
            id="summary-findings"
            title="Key findings at a glance"
          >
            <ol className="space-y-3">
              {summary.key_findings.map((item, index) => (
                <li
                  key={item.statement}
                  className="flex gap-3"
                >
                  <span className="bg-primary text-primary-foreground mt-0.5 flex size-6 shrink-0 items-center justify-center rounded-full text-xs font-semibold tabular-nums">
                    {index + 1}
                  </span>
                  <CitationText
                    markdown={item.statement}
                    className="min-w-0 flex-1"
                  />
                </li>
              ))}
            </ol>
          </Section>
        ) : null}
        {summary.candidates.length ? (
          <Section
            id="summary-candidates"
            title="Top candidates"
          >
            <div className="space-y-3">
              {summary.candidates.map((item) => (
                <Candidate
                  key={item.gene_id}
                  report={report}
                  item={item}
                />
              ))}
            </div>
          </Section>
        ) : null}
        {summary.loci.length ? (
          <Section
            id="summary-loci"
            title="Locus by locus"
          >
            <dl className="space-y-3">
              {summary.loci.map((item) => (
                <div
                  key={item.locus_id}
                  className="flex gap-3"
                >
                  <dt className="w-8 shrink-0 pt-0.5 font-mono text-sm font-semibold">
                    {item.locus_id}
                  </dt>
                  <dd className="min-w-0 flex-1">
                    <CitationText markdown={item.narrative} />
                  </dd>
                </div>
              ))}
            </dl>
          </Section>
        ) : null}
        {summary.caveats.length || summary.next_steps.length ? (
          <div className="grid gap-6 md:grid-cols-2">
            {summary.caveats.length ? (
              <Section
                id="summary-caveats"
                title="Confidence and caveats"
              >
                <ul className="text-muted-foreground list-disc space-y-2 pl-5 text-sm">
                  {summary.caveats.map((item) => (
                    <li key={item}>
                      <CitationText markdown={item} />
                    </li>
                  ))}
                </ul>
              </Section>
            ) : null}
            {summary.next_steps.length ? (
              <Section
                id="summary-next-steps"
                title="Recommended next steps"
              >
                <ul className="list-disc space-y-2 pl-5 text-sm">
                  {summary.next_steps.map((item) => (
                    <li key={item}>
                      <CitationText markdown={item} />
                    </li>
                  ))}
                </ul>
              </Section>
            ) : null}
          </div>
        ) : null}
      </article>
      {summary.candidates.length ? (
        <AtAGlance
          report={report}
          summary={summary}
        />
      ) : null}
    </div>
  );
}
