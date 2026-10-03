import { describe, expect, it } from "vitest";
import type { SpeciesInfo } from "@/lib/study-api";
import {
  ldAvailable,
  studyFormSchema,
  studySummary,
  toStudyRequest,
  type StudyFormValues,
} from "./study-schema";

function soybean(ld: SpeciesInfo["ld"]): SpeciesInfo {
  return {
    species: "soybean",
    scientific_name: "Glycine max",
    common_name: "soybean",
    taxon_id: 3847,
    canonical_assembly: "Wm82.a2.v1",
    default_window: { flank_bp: 250_000, warn_above_bp: null },
    typical_ld_kb: 150,
    ld_note: "",
    assemblies: [
      {
        id: "Wm82.a2.v1",
        aliases: [],
        description: "",
        canonical: true,
        chromosomes: [],
      },
    ],
    chromosome_prefixes: ["Gm"],
    linkouts: [],
    tiers: {},
    bundle: null,
    ld,
    sources: [],
  };
}

const values: StudyFormValues = {
  mode: "snps",
  species: "soybean",
  assembly: "Wm82.a2.v1",
  trait_text: "plant height",
  window_kb: 250,
  window_mode: "ld",
  ld_r2: 0.3,
  top_k_per_locus: 5,
  specialists_enabled: ["locus_variant"],
  assembly_confirmed: true,
  snps: [{ raw: "S18_9263941", chrom: "18", pos: 9_263_941 }],
};

describe("LD window mode", () => {
  it("is offered only when a panel and PLINK2 are available", () => {
    expect(
      ldAvailable(
        soybean({ available: true, panels: ["Song_Hyten_2015"], reason: null }),
      ),
    ).toBe(true);
    expect(
      ldAvailable(
        soybean({
          available: false,
          panels: [],
          reason: "PLINK2 is not installed",
        }),
      ),
    ).toBe(false);
    expect(ldAvailable(soybean(undefined))).toBe(false);
  });

  it("sends mode ld with the r² threshold and keeps the fixed flank as fallback", () => {
    const study = toStudyRequest(values);
    expect(study.window).toEqual({ mode: "ld", flank_bp: 250_000, r2: 0.3 });
    expect(studySummary(study)).toContain(
      "LD windows (r² ≥ 0.3, fixed ±250 kb fallback)",
    );
    expect(toStudyRequest({ ...values, window_mode: "fixed" }).window).toEqual({
      mode: "fixed",
      flank_bp: 250_000,
    });
  });

  it("rejects LD mode when the registry says LD is unavailable", () => {
    const blocked = studyFormSchema([
      soybean({
        available: false,
        panels: [],
        reason: "PLINK2 is not installed",
      }),
    ]).safeParse(values);
    expect(blocked.success).toBe(false);
    expect(blocked.error?.issues[0]?.path).toEqual(["window_mode"]);
    expect(blocked.error?.issues[0]?.message).toContain(
      "PLINK2 is not installed",
    );
    const allowed = studyFormSchema([
      soybean({ available: true, panels: ["Song_Hyten_2015"], reason: null }),
    ]).safeParse(values);
    expect(allowed.success).toBe(true);
  });
});

describe("sister-team files", () => {
  it("cannot start until the assembly of the positions is chosen", () => {
    const schema = studyFormSchema([
      soybean({ available: true, panels: ["Song_Hyten_2015"], reason: null }),
    ]);
    const waiting = schema.safeParse({ ...values, assembly_confirmed: false });
    expect(waiting.success).toBe(false);
    expect(waiting.error?.issues[0]).toMatchObject({
      path: ["assembly"],
      message: "Choose the assembly the sister-team positions are on",
    });
    expect(toStudyRequest(values)).not.toHaveProperty("assembly_confirmed");
  });
});
