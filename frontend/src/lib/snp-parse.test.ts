import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";
import {
  MAX_INPUT_BYTES,
  OUT_OF_BOUNDS_WARN_SHARE,
  assemblyFit,
  chromosomeNormalizer,
  mergeValidation,
  parseSnpText,
  rejectedRowsCsv,
  submittableSnps,
  type ParseOptions,
} from "./snp-parse";
import type { StudyValidation } from "./study-api";

const here = path.dirname(fileURLToPath(import.meta.url));
const soybean = JSON.parse(
  fs.readFileSync(
    path.join(here, "__fixtures__", "soybean-wm82-a2.json"),
    "utf8",
  ),
) as {
  id: string;
  prefixes: string[];
  chromosomes: { name: string; length: number; aliases: string[] }[];
};
const options: ParseOptions = { assembly: soybean, prefixes: soybean.prefixes };
const sisterTeamDir = path.resolve(here, "../../../../Results/2_Sep/Lee");

function placed(text: string) {
  return parseSnpText(text, options).rows.map((row) => [
    row.raw,
    row.status,
    row.chrom,
    row.pos,
  ]);
}

describe("pasted SNP lists", () => {
  it("accepts every positional spelling and normalizes chromosome aliases", () => {
    expect(
      placed(
        [
          "S18_9263941",
          "Chr18:9263941",
          "chr5_2899164",
          "18 51620945",
          "5,3000000",
          "Gm07:100",
          "gm7 200",
          "chromosome 07:300",
          "\t20\t1000",
        ].join("\n"),
      ),
    ).toEqual([
      ["S18_9263941", "ok", "Gm18", 9_263_941],
      ["Chr18:9263941", "warning", "Gm18", 9_263_941],
      ["chr5_2899164", "ok", "Gm05", 2_899_164],
      ["18 51620945", "ok", "Gm18", 51_620_945],
      ["5,3000000", "ok", "Gm05", 3_000_000],
      ["Gm07:100", "ok", "Gm07", 100],
      ["gm7 200", "ok", "Gm07", 200],
      ["chromosome", "needs_lookup", null, null],
      ["07:300", "ok", "Gm07", 300],
      ["20\t1000", "ok", "Gm20", 1000],
    ]);
  });

  it("flags marker ids for lookup and splits several ids on one line", () => {
    const result = parseSnpText(
      "ss715631025, rs123456; BARC_1.01_Gm18_9199987_A_G\nSatt324",
      options,
    );
    expect(result.rows.map((row) => [row.raw, row.status])).toEqual([
      ["ss715631025", "needs_lookup"],
      ["rs123456", "needs_lookup"],
      ["BARC_1.01_Gm18_9199987_A_G", "needs_lookup"],
      ["Satt324", "needs_lookup"],
    ]);
    expect(submittableSnps(result.rows)[0]).toEqual({
      raw: "ss715631025",
      marker_id: "ss715631025",
    });
    expect(result.summary).toMatchObject({ needsLookup: 4, valid: 0 });
  });

  it("dedupes by canonical position and rejects invalid rows", () => {
    const result = parseSnpText(
      [
        "S18_9263941",
        "Gm18:9263941",
        "S99_10",
        "S18_99999999",
        "18:0",
        "@@@",
        "# comment",
        "",
        "ss1",
        "SS1",
      ].join("\n"),
      options,
    );
    expect(
      result.rows.map((row) => [row.line, row.status, row.issues[0]?.code]),
    ).toEqual([
      [1, "ok", undefined],
      [2, "warning", "duplicate"],
      [3, "invalid", "unknown_chromosome"],
      [4, "invalid", "out_of_bounds"],
      [5, "invalid", "bad_position"],
      [6, "invalid", "unparsed"],
      [9, "needs_lookup", "needs_lookup"],
      [10, "warning", "needs_lookup"],
    ]);
    expect(result.rows[1].duplicateOf).toBe(1);
    expect(result.rows[3].issues[0].message).toContain("58,018,742 bp");
    expect(result.summary).toEqual({
      total: 8,
      valid: 1,
      duplicates: 2,
      invalid: 4,
      needsLookup: 1,
      warnings: 0,
    });
    expect(submittableSnps(result.rows)).toEqual([
      { raw: "S18_9263941", chrom: "Gm18", pos: 9_263_941 },
      { raw: "ss1", marker_id: "ss1" },
    ]);
    const csv = rejectedRowsCsv(result.rows).split("\r\n");
    expect(csv[0]).toBe("line,snp,status,reason");
    expect(csv).toHaveLength(7);
    expect(csv[1]).toBe(
      "2,Gm18:9263941,duplicate,same SNP as line 1; kept once",
    );
  });

  it("keeps a chromosome unchecked until the assembly is known", () => {
    const result = parseSnpText("Chr18:9263941", {});
    expect(result.rows[0]).toMatchObject({ status: "warning", chrom: "Chr18" });
    expect(chromosomeNormalizer(null).checked).toBe(false);
  });

  it("reads headerless id,chrom,pos lines and ignores trailing p-values", () => {
    expect(placed("S5_2899164,5,2899164\nS18_9263941 0.00001")).toEqual([
      ["S5_2899164", "ok", "Gm05", 2_899_164],
      ["S18_9263941", "ok", "Gm18", 9_263_941],
    ]);
  });

  it("refuses input above 5 MB", () => {
    const result = parseSnpText("S18_9263941\n".repeat(MAX_INPUT_BYTES / 10));
    expect(result.error).toContain("5 MB");
    expect(result.rows).toEqual([]);
  });
});

describe("tables", () => {
  it("detects headers and maps common column names", () => {
    const tsv =
      "SNP\tCHR\tBP\tP\nS5_2899164\t5\t2899164\t1e-8\nmarker\t18\t9263941\t0.2\n";
    const result = parseSnpText(tsv, options);
    expect(result.format).toBe("table");
    expect(result.mapping).toEqual({ id: 0, chrom: 1, pos: 2, p_value: 3 });
    expect(submittableSnps(result.rows)).toEqual([
      { raw: "S5_2899164", chrom: "Gm05", pos: 2_899_164, p_value: 1e-8 },
      { raw: "marker", chrom: "Gm18", pos: 9_263_941, p_value: 0.2 },
    ]);
  });

  it("asks for a column mapping when headers are unknown", () => {
    const csv = "locus_label,linkage_group,coordinate_bp\nq1,18,9263941\n";
    const unmapped = parseSnpText(csv, options);
    expect(unmapped).toMatchObject({
      format: "table",
      needsMapping: true,
      rows: [],
    });
    expect(unmapped.header).toEqual([
      "locus_label",
      "linkage_group",
      "coordinate_bp",
    ]);
    const mapped = parseSnpText(csv, {
      ...options,
      mapping: { id: 0, chrom: 1, pos: 2 },
    });
    expect(mapped.needsMapping).toBe(false);
    expect(submittableSnps(mapped.rows)).toEqual([
      { raw: "q1", chrom: "Gm18", pos: 9_263_941 },
    ]);
  });

  it("parses the sister-team preset with scores, methods and mismatches", () => {
    const text = fs.readFileSync(
      path.join(here, "__fixtures__", "sister-team-sample.csv"),
      "utf8",
    );
    const result = parseSnpText(text, options);
    expect(result.preset).toBe("sister-team");
    expect(result.mapping).toEqual({
      id: 0,
      chrom: 1,
      pos: 2,
      score: 4,
      method: 6,
    });
    expect(result.rows.map((row) => [row.raw, row.status])).toEqual([
      ["S18_9263941", "ok"],
      ["S5_2899164", "ok"],
      ["S18_51620945", "ok"],
      ["S18_9263941", "warning"],
      ["S18_99999999", "invalid"],
      ["S7_1000", "warning"],
    ]);
    expect(result.rows[5].issues[0].code).toBe("position_mismatch");
    expect(submittableSnps(result.rows)[0]).toEqual({
      raw: "S18_9263941",
      chrom: "Gm18",
      pos: 9_263_941,
      score: 0.0199,
      method: "gnnexplainer",
    });
    expect(result.summary).toMatchObject({
      valid: 4,
      duplicates: 1,
      invalid: 1,
      warnings: 1,
    });
  });

  it.skipIf(!fs.existsSync(sisterTeamDir))(
    "parses every real sister-team CSV in Results/2_Sep/Lee",
    () => {
      const files = fs
        .readdirSync(sisterTeamDir)
        .filter((name) => name.endsWith(".csv"));
      expect(files.length).toBeGreaterThan(0);
      for (const name of files) {
        const text = fs.readFileSync(path.join(sisterTeamDir, name), "utf8");
        const result = parseSnpText(text, options);
        expect(result.preset, name).toBe("sister-team");
        expect(result.rows, name).toHaveLength(50);
        expect(
          new Set(
            result.rows
              .filter((row) => row.status === "invalid")
              .map((row) => row.issues[0]?.code),
          ),
          name,
        ).toEqual(new Set(result.summary.invalid ? ["out_of_bounds"] : []));
        expect(
          result.summary.valid +
            result.summary.duplicates +
            result.summary.invalid,
          name,
        ).toBe(50);
        expect(result.summary.valid, name).toBeGreaterThan(30);
        for (const row of result.rows) {
          if (row.status !== "invalid") {
            expect(row.chrom, name).toMatch(/^Gm(0[1-9]|1\d|20)$/);
          }
          expect(row.score, name).toBeGreaterThan(0);
          expect(row.method, name).toBe("gnnexplainer");
          expect(
            row.issues.map((issue) => issue.code),
            name,
          ).not.toContain("position_mismatch");
        }
      }
    },
  );

  it.skipIf(!fs.existsSync(sisterTeamDir))(
    "finds the sister-team positions past Wm82.a2 ends and all within Lee.gnm2",
    () => {
      const text = fs
        .readdirSync(sisterTeamDir)
        .filter((name) => name.endsWith(".csv"))
        .map((name, index) => {
          const body = fs.readFileSync(path.join(sisterTeamDir, name), "utf8");
          return index === 0 ? body : body.split("\n").slice(1).join("\n");
        })
        .join("\n");
      const result = parseSnpText(text, options);
      const leeGnm2 = JSON.parse(
        fs.readFileSync(
          path.join(here, "__fixtures__", "soybean-lee-gnm2.json"),
          "utf8",
        ),
      );
      const [wm82, lee] = assemblyFit(
        result.rows,
        [soybean, leeGnm2],
        soybean.prefixes,
      );
      expect(wm82.checked).toBe(600);
      expect(wm82.beyond).toBe(64);
      expect(wm82.share).toBeGreaterThan(OUT_OF_BOUNDS_WARN_SHARE);
      expect(lee).toMatchObject({ assembly: "Lee.gnm2", beyond: 0, share: 0 });
    },
  );
});

describe("VCF", () => {
  it("reads CHROM, POS and ID and labels unnamed variants", () => {
    const vcf = [
      "##fileformat=VCFv4.2",
      "##contig=<ID=Gm18>",
      "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO",
      "Gm18\t9263941\tS18_9263941\tA\tG\t.\tPASS\t.",
      "chr05\t2899164\t.\tC\tT\t.\tPASS\t.",
      "scaffold_9\t10\t.\tC\tT\t.\tPASS\t.",
    ].join("\n");
    const result = parseSnpText(vcf, options);
    expect(result.format).toBe("vcf");
    expect(result.rows.map((row) => [row.line, row.raw, row.status])).toEqual([
      [4, "S18_9263941", "ok"],
      [5, "chr05:2899164", "ok"],
      [6, "scaffold_9:10", "invalid"],
    ]);
  });
});

describe("server preview merge", () => {
  it("adopts the server's placements, warnings and located errors", () => {
    const parsed = parseSnpText(
      "S18_9263941\nss715631025\nBARC_123\nS5_2899164",
      options,
    );
    const validation: StudyValidation = {
      study_normalized: { species: "soybean" },
      placed_snps: [
        { raw: "S18_9263941", chrom: "Gm18", pos: 9_263_941 },
        { raw: "ss715631025", chrom: "Gm18", pos: 9_250_001 },
      ],
      warnings: [
        {
          code: "unresolved_marker",
          message: "BARC_123 could not be placed: not found on Wm82.a2.v1",
          snp: "BARC_123",
        },
      ],
      errors: [{ loc: ["snps", 3, "pos"], message: "Input should be >= 1" }],
      detail: "",
      preview: { loci: [], genes: 0 },
    };
    const rows = mergeValidation(parsed.rows, validation);
    expect(
      rows.map((row) => [row.raw, row.status, row.server, row.chrom, row.pos]),
    ).toEqual([
      ["S18_9263941", "ok", "placed", "Gm18", 9_263_941],
      ["ss715631025", "ok", "placed", "Gm18", 9_250_001],
      ["BARC_123", "invalid", "dropped", null, null],
      ["S5_2899164", "invalid", "dropped", "Gm05", 2_899_164],
    ]);
    expect(rows[2].issues.at(-1)?.message).toContain("could not be placed");
    expect(rows[3].issues.at(-1)?.message).toBe("Input should be >= 1");
    expect(mergeValidation(parsed.rows, null)[0].server).toBeNull();
  });
});
