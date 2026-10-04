import { describe, expect, it } from "vitest";
import type { StudyReport } from "@/lib/study-api";
import {
  candidatesCsv,
  citationIndex,
  evidenceCsv,
  geneLinks,
  linkCitations,
  reportJson,
  reportMarkdown,
  unresolvedCitations,
} from "./exports";

describe("linkCitations", () => {
  it("turns citation markers into cite links and leaves real links alone", () => {
    expect(
      linkCitations(
        "| L2 | supports [E12] [E3] |\nSee [E7](#cite-E7) and [docs](https://x.org).",
      ),
    ).toBe(
      "| L2 | supports [E12](#cite-E12) [E3](#cite-E3) |\nSee [E7](#cite-E7) and [docs](https://x.org).",
    );
  });

  it("links source citations too", () => {
    expect(linkCitations("From [source:atted_soybean] and [E2].")).toBe(
      "From [source:atted_soybean](#cite-source:atted_soybean) and [E2](#cite-E2).",
    );
  });
});

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
      gene_id: "Glyma.18G092200",
      symbol: "WRKY",
      locus_id: "L1",
      tier: "T4",
      score: 20,
      distance_bp: 0,
      overlaps_snp: true,
      shortlist: true,
      verifier_status: "unverified",
      category_points: { A: 20, E: 0 },
    },
  ],
  citations: [
    {
      alias: "E1",
      evidence_id: "abc",
      gene_id: "Glyma.18G092200",
      source_db: "LIS",
      category: "positional",
      subtype: "in_window",
      quote: "The lead SNP overlaps the gene.",
      verifier_status: "unverified",
    },
  ],
  warnings: [],
  limitations: [],
  evidence_count: 1,
  finding_count: 0,
  markdown: "Supported by [E1]. Not [E9999].",
  provenance: { rubric_version: 3 },
};

describe("report exports", () => {
  it("writes candidate and evidence CSV columns", () => {
    const candidates = candidatesCsv(report).split("\n");
    expect(candidates[0]?.split(",")).toEqual([
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
      "A",
      "B",
      "C",
      "D",
      "E",
      "F",
      "G",
    ]);
    expect(candidates[1]).toContain("Glyma.18G092200");
    const evidence = evidenceCsv(report).split("\n");
    expect(evidence[0]).toBe(
      "gene_id,alias,evidence_id,category,subtype,source_db,verifier_status,quote",
    );
    expect(evidence[1]).toContain("The lead SNP overlaps the gene.");
  });

  it("exports JSON with the study, report and provenance", () => {
    const parsed = JSON.parse(reportJson(report)) as {
      schema: string;
      study: { trait: string };
      provenance: { rubric_version: number };
    };
    expect(parsed.schema).toBe("agrihub.report/v1");
    expect(parsed.study.trait).toBe("plant height");
    expect(parsed.provenance.rubric_version).toBe(3);
  });

  it("exports the written summary followed by the detailed tables", () => {
    const markdown = reportMarkdown({
      ...report,
      markdown: "# Title\n\n## Bottom line\n\nGene A leads [E1].",
      details_markdown: "# Title\n\n| Locus | Region |\n| --- | --- |",
    });
    expect(markdown).toBe(
      "# Title\n\n## Bottom line\n\nGene A leads [E1].\n\n## Detailed tables\n\n| Locus | Region |\n| --- | --- |\n",
    );
  });

  it("builds gene links from the report's link-out templates", () => {
    expect(
      geneLinks(
        {
          ...report,
          provenance: {
            gene_linkouts: [
              { name: "SoyBase", template: "https://soy.org/?q={gene_id}" },
              { name: "broken" },
            ],
          },
        },
        "Glyma.18G092200",
      ),
    ).toEqual([
      { name: "SoyBase", href: "https://soy.org/?q=Glyma.18G092200" },
    ]);
    expect(geneLinks(report, "Glyma.18G092200")).toEqual([]);
  });

  it("resolves citations that are in the report index", () => {
    const known = new Set(citationIndex(report).keys());
    expect(unresolvedCitations(report.markdown ?? "", known)).toEqual([
      "E9999",
    ]);
    expect(citationIndex(report).get("E1")?.quote).toContain("overlaps");
  });
});
