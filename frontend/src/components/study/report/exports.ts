import type { CandidateRow, Tier } from "@/lib/run-events";
import type {
  ReportCitation,
  ReportSource,
  StudyReport,
} from "@/lib/study-api";

const CATEGORIES = ["A", "B", "C", "D", "E", "F", "G"] as const;

export const TIER_VARIANT: Record<
  Tier,
  "success" | "info" | "warning" | "outline"
> = {
  T1: "success",
  T2: "info",
  T3: "warning",
  T4: "outline",
};

export function sourceIndex(report: StudyReport): Map<string, ReportSource> {
  return new Map((report.sources ?? []).map((item) => [item.source_id, item]));
}

export function reportCandidates(report: StudyReport): CandidateRow[] {
  return report.candidates_full?.length
    ? report.candidates_full
    : report.candidates;
}

export function citationIndex(
  report: StudyReport,
): Map<string, ReportCitation> {
  return new Map((report.citations ?? []).map((item) => [item.alias, item]));
}

export function unresolvedCitations(
  markdown: string,
  known: Set<string>,
): string[] {
  const orphans: string[] = [];
  for (const match of markdown.matchAll(/\[(E\d+)\]/g)) {
    const token = match[1];
    if (token && !known.has(token)) {
      orphans.push(token);
    }
  }
  return orphans;
}

export const CITATION_HREF = "#cite-";
export const SOURCE_PREFIX = "source:";

/**
 * Rewrite `[E12]` and `[source:atted]` citation markers as `#cite-…` links so
 * Markdown renders them inline.
 */
export function linkCitations(markdown: string): string {
  return markdown.replace(
    /\[(E\d+|source:[A-Za-z0-9_.-]+)\](?!\()/g,
    `[$1](${CITATION_HREF}$1)`,
  );
}

export type GeneLink = { name: string; href: string };

/** Return the species' gene pages for one gene, from the report's link-out templates. */
export function geneLinks(report: StudyReport, geneId: string): GeneLink[] {
  const templates = report.provenance.gene_linkouts;
  if (!Array.isArray(templates)) {
    return [];
  }
  return templates.flatMap((item: { name?: unknown; template?: unknown }) =>
    typeof item?.name === "string" && typeof item.template === "string"
      ? [
          {
            name: item.name,
            href: item.template.replaceAll(
              "{gene_id}",
              encodeURIComponent(geneId),
            ),
          },
        ]
      : [],
  );
}

export function download(filename: string, contents: string, type: string) {
  const blob = new Blob([contents], { type });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  link.click();
  URL.revokeObjectURL(url);
}

export function reportJson(report: StudyReport): string {
  return JSON.stringify(
    {
      schema: "agrihub.report/v1",
      study: {
        species: report.species,
        assembly: report.assembly,
        trait: report.trait,
        mode: report.mode,
      },
      report,
      provenance: report.provenance,
    },
    null,
    2,
  );
}

function csvCell(value: unknown): string {
  const text = value == null ? "" : String(value);
  if (/[",\n]/.test(text)) {
    return `"${text.replaceAll('"', '""')}"`;
  }
  return text;
}

function csv(rows: unknown[][]): string {
  return rows.map((row) => row.map(csvCell).join(",")).join("\n");
}

export function candidatesCsv(report: StudyReport): string {
  const header = [
    "rank",
    "gene_id",
    "symbol",
    "locus_id",
    "tier",
    "score",
    "distance_bp",
    "overlaps_snp",
    "shortlist",
    "verifier_status",
    ...CATEGORIES,
  ];
  const body = reportCandidates(report).map((item) => {
    const points = item.category_points ?? {};
    return [
      item.rank ?? "",
      item.gene_id,
      item.symbol ?? "",
      item.locus_id,
      item.tier,
      item.score,
      item.distance_bp ?? "",
      item.overlaps_snp ? "true" : "false",
      item.shortlist ? "true" : "false",
      item.verifier_status ?? "",
      ...CATEGORIES.map((code) => points[code] ?? 0),
    ];
  });
  return csv([header, ...body]);
}

export function evidenceCsv(report: StudyReport): string {
  const header = [
    "gene_id",
    "alias",
    "evidence_id",
    "category",
    "subtype",
    "source_db",
    "verifier_status",
    "quote",
  ];
  const byGene = new Map(
    reportCandidates(report).map((item) => [item.gene_id, item]),
  );
  const body = (report.citations ?? []).map((item) => [
    item.gene_id,
    item.alias,
    item.evidence_id,
    item.category,
    item.subtype,
    item.source_db,
    item.verifier_status ?? byGene.get(item.gene_id)?.verifier_status ?? "",
    item.quote ?? "",
  ]);
  return csv([header, ...body]);
}

/** Return the written summary followed by the full per-locus tables. */
export function reportMarkdown(report: StudyReport): string {
  const summary = report.markdown?.trim() ?? "";
  const details = (report.details_markdown?.trim() ?? "").replace(
    /^# .*\n?/,
    "",
  );
  if (!summary && !details) {
    return `# ${report.title}\n\n${report.limitations.map((item) => `- ${item}`).join("\n")}\n`;
  }
  if (!details) {
    return `${summary}\n`;
  }
  return `${summary || `# ${report.title}`}\n\n## Detailed tables\n\n${details.trim()}\n`;
}
