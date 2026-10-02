import type { CandidateRow } from "@/lib/run-events";
import type { ReportCitation, StudyReport } from "@/lib/study-api";

const CATEGORIES = ["A", "B", "C", "D", "E", "F", "G"] as const;

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

export function reportMarkdown(report: StudyReport): string {
  return report.markdown?.trim()
    ? report.markdown
    : `# ${report.title}\n\n${report.limitations.map((item) => `- ${item}`).join("\n")}\n`;
}
