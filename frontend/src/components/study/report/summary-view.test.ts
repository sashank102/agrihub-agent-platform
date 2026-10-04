import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import type { StudyReport } from "@/lib/study-api";
import { CitationProvider } from "./citation-text";
import { citationIndex, sourceIndex } from "./exports";
import { SummaryView } from "./summary-view";

const report: StudyReport = {
  title: "plant height candidate genes in soybean",
  species: "soybean",
  assembly: "Wm82.a2.v1",
  trait: "plant height",
  mode: "snps",
  loci: [],
  candidates: [],
  candidates_full: [
    {
      rank: 1,
      gene_id: "Glyma.19G194300",
      symbol: "GmDT1",
      locus_id: "L3",
      tier: "T1",
      score: 61.2,
      share_of_locus: 0.77,
    },
  ],
  citations: [
    {
      alias: "E14",
      evidence_id: "ev14",
      gene_id: "Glyma.19G194300",
      source_db: "LIS",
      category: "curated",
      subtype: "trait_gene",
      quote: "GmDT1 controls stem growth habit.",
    },
  ],
  sources: [{ source_id: "lis", name: "LIS trait genes", version: "2024" }],
  warnings: [],
  limitations: [],
  evidence_count: 14,
  finding_count: 2,
  summary: {
    bottom_line:
      "Glyma.19G194300 (GmDT1) is the strongest plant height candidate [E14].",
    key_findings: [
      {
        statement:
          "**GmDT1 leads the study.** It is a curated gene [source:lis].",
        citations: ["source:lis"],
      },
    ],
    candidates: [
      {
        gene_id: "Glyma.19G194300",
        symbol: "GmDT1",
        locus_id: "L3",
        tier: "T1",
        confidence: "strong",
        narrative: "It lies 58 kb from S19_45243000 [E14].",
        citations: ["E14"],
      },
    ],
    loci: [
      {
        locus_id: "L3",
        narrative: "GmDT1 leads L3 with a clear lead.",
        citations: [],
      },
    ],
    caveats: ["Expression data comes from one atlas."],
    next_steps: ["Test a Glyma.19G194300 knockout for plant height."],
    written_by: "anthropic:claude-haiku-4-5",
  },
  markdown: "# fallback markdown",
  provenance: {
    gene_linkouts: [
      {
        name: "SoyBase",
        template: "https://soybase.org/gene/{gene_id}",
      },
      {
        name: "Ensembl Plants",
        template: "https://plants.ensembl.org/g={gene_id}",
      },
    ],
  },
};

function render(value: StudyReport): string {
  return renderToStaticMarkup(
    createElement(CitationProvider, {
      value: {
        citations: citationIndex(value),
        sources: sourceIndex(value),
        onOpenEvidence: () => undefined,
        onOpenSource: () => undefined,
      },
      children: createElement(SummaryView, { report: value }),
    }),
  );
}

describe("SummaryView", () => {
  it("renders every section of the written summary, bottom line first", () => {
    const html = render(report);
    const order = [
      "Bottom line",
      "Key findings at a glance",
      "Top candidates",
      "Locus by locus",
      "Confidence and caveats",
      "Recommended next steps",
    ].map((heading) => html.indexOf(heading));
    expect(order.every((index) => index >= 0)).toBe(true);
    expect([...order].sort((left, right) => left - right)).toEqual(order);
    expect(html).toContain("is the strongest plant height candidate");
    expect(html).toContain("<strong>GmDT1 leads the study.</strong>");
    expect(html).toContain("strong confidence");
    expect(html).toContain("Expression data comes from one atlas.");
    expect(html).not.toContain("fallback markdown");
  });

  it("links genes, evidence and sources", () => {
    const html = render(report);
    expect(html).toContain('href="https://soybase.org/gene/Glyma.19G194300"');
    expect(html).toContain('data-citation="E14"');
    expect(html).toContain('data-source-citation="lis"');
    expect(html).toContain("[LIS trait genes]");
    expect(html).toContain("Candidates at a glance");
    expect(html).toContain("77%");
  });

  it("falls back to the report Markdown when there is no structured summary", () => {
    const html = render({ ...report, summary: null });
    expect(html).toContain("fallback markdown");
    expect(html).not.toContain("Key findings at a glance");
  });
});
