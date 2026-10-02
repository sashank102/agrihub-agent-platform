import { z } from "zod";
import { SPECIALISTS, type SpecialistName } from "@/lib/run-events";
import type { SpeciesInfo, StudyRequest } from "@/lib/study-api";

export const SPECIALIST_LABELS: Record<SpecialistName, string> = {
  locus_variant: "Locus and variant",
  qtl_gwas: "QTL and GWAS",
  function_orthology: "Function and orthology",
  expression_network: "Expression and network",
  literature: "Literature",
};

export const TRAIT_SUGGESTIONS: Record<string, string[]> = {
  soybean: [
    "plant height",
    "flowering time",
    "maturity",
    "seed protein content",
    "seed oil content",
    "seed yield",
    "100-seed weight",
    "lodging",
    "pod shattering",
    "canopy temperature",
    "nodule number",
  ],
  maize: [
    "plant height",
    "days to silking",
    "kernel row number",
    "ear length",
    "grain yield",
    "drought tolerance",
  ],
  rice: [
    "plant height",
    "heading date",
    "grain length",
    "grain width",
    "tiller number",
    "salt tolerance",
  ],
  sorghum: [
    "plant height",
    "flowering time",
    "grain yield",
    "stay-green",
    "panicle length",
  ],
};

const snpInput = z.object({
  raw: z.string().min(1),
  marker_id: z.string().nullish(),
  chrom: z.string().nullish(),
  pos: z.number().int().min(1).nullish(),
  score: z.number().nullish(),
  p_value: z.number().min(0).max(1).nullish(),
  method: z.string().nullish(),
});

const base = z.object({
  species: z.string().min(1, "Choose a species"),
  assembly: z.string().min(1, "Choose an assembly"),
  trait_text: z.string().trim().min(1, "Describe the trait"),
  window_kb: z
    .number({ error: "Enter a window size in kb" })
    .int()
    .min(1, "The window must be at least 1 kb")
    .max(5000, "The window can be at most 5,000 kb"),
  window_mode: z.enum(["fixed", "ld"]),
  ld_r2: z
    .number({ error: "Enter an r² threshold" })
    .min(0.05, "r² must be at least 0.05")
    .max(1, "r² can be at most 1"),
  top_k_per_locus: z.number().int().min(1).max(50),
  specialists_enabled: z
    .array(z.enum(SPECIALISTS))
    .min(1, "Enable at least one specialist"),
});

export function studyFormSchema(registry: SpeciesInfo[]) {
  return z
    .discriminatedUnion("mode", [
      base.extend({
        mode: z.literal("snps"),
        snps: z
          .array(snpInput)
          .min(1, "Add at least one SNP that can be placed"),
      }),
      base.extend({
        mode: z.literal("trait"),
        snps: z.array(snpInput).optional(),
      }),
    ])
    .superRefine((value, context) => {
      if (registry.length === 0) {
        return;
      }
      const species = registry.find((item) => item.species === value.species);
      if (!species) {
        context.addIssue({
          code: "custom",
          path: ["species"],
          message: `${value.species} is not a registered species`,
        });
        return;
      }
      if (!species.assemblies.some((item) => item.id === value.assembly)) {
        context.addIssue({
          code: "custom",
          path: ["assembly"],
          message: `${value.assembly} is not a ${species.common_name} assembly`,
        });
      }
      if (value.window_mode === "ld" && !ldAvailable(species)) {
        context.addIssue({
          code: "custom",
          path: ["window_mode"],
          message: `LD windows are unavailable for ${species.common_name}: ${species.ld?.reason ?? "no LD panel is built"}`,
        });
      }
    });
}

export type StudyFormValues = z.infer<ReturnType<typeof studyFormSchema>>;

export function defaultWindowKb(species: SpeciesInfo | undefined): number {
  const flank = species?.default_window.flank_bp;
  return flank ? Math.round(flank / 1000) : 100;
}

export function ldWarningKb(species: SpeciesInfo | undefined): number | null {
  return species ? 2 * species.typical_ld_kb : null;
}

export function ldAvailable(species: SpeciesInfo | undefined): boolean {
  return Boolean(species?.ld?.available && species.ld.panels.length > 0);
}

export function toStudyRequest(values: StudyFormValues): StudyRequest {
  const common = {
    species: values.species,
    assembly: values.assembly,
    trait_text: values.trait_text.trim(),
    window:
      values.window_mode === "ld"
        ? {
            mode: "ld" as const,
            flank_bp: values.window_kb * 1000,
            r2: values.ld_r2,
          }
        : { mode: "fixed" as const, flank_bp: values.window_kb * 1000 },
    top_k_per_locus: values.top_k_per_locus,
    specialists_enabled: values.specialists_enabled,
  };
  if (values.mode === "snps") {
    return { ...common, mode: "snps", snps: values.snps };
  }
  return { ...common, mode: "trait" };
}

export function studySummary(study: StudyRequest): string {
  const snps =
    study.mode === "snps"
      ? `${study.snps.length} SNP${study.snps.length === 1 ? "" : "s"}`
      : "trait mode";
  const windows =
    study.window.mode === "ld"
      ? `LD windows (r² ≥ ${study.window.r2}, fixed ±${study.window.flank_bp / 1000} kb fallback)`
      : `±${study.window.flank_bp / 1000} kb windows`;
  return `Study: ${study.trait_text} in ${study.species} (${study.assembly}), ${snps}, ${windows}.`;
}
