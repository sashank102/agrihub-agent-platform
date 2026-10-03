import type { Thread } from "@langchain/langgraph-sdk";
import { useCallback, useMemo } from "react";
import { useApiKey } from "./api-key";
import type {
  CandidateRow,
  LocusRow,
  SpecialistName,
  StudyInputIssue,
  StudyWarning,
} from "./run-events";

export const API_URL =
  process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
export const STUDY_ASSISTANT_ID =
  process.env.NEXT_PUBLIC_ASSISTANT_ID || "agrihub_study";
export const CHAT_ASSISTANT_ID =
  process.env.NEXT_PUBLIC_CHAT_ASSISTANT_ID || "agrihub";

export type Chromosome = { name: string; length: number; aliases: string[] };

export type Assembly = {
  id: string;
  aliases: string[];
  description: string;
  canonical: boolean;
  /** Positions on this assembly are lifted to ``lift_to`` before loci are built. */
  lift_to?: string | null;
  chromosomes: Chromosome[];
};

export type SpeciesInfo = {
  species: string;
  scientific_name: string;
  common_name: string;
  taxon_id: number;
  canonical_assembly: string;
  default_window: { flank_bp: number | null; warn_above_bp: number | null };
  typical_ld_kb: number;
  ld_note: string;
  assemblies: Assembly[];
  chromosome_prefixes: string[];
  linkouts: Record<string, unknown>[];
  tiers: Record<string, { sources: number; built: boolean }>;
  bundle: { tier: string; built_at: string } | null;
  ld?: { available: boolean; panels: string[]; reason: string | null };
  sources: { id: string; name: string; tier: string; status: string }[];
};

export type SnpInput = {
  raw: string;
  marker_id?: string | null;
  chrom?: string | null;
  pos?: number | null;
  score?: number | null;
  p_value?: number | null;
  method?: string | null;
  score_type?: string | null;
  model_id?: string | null;
  lifted_from?: {
    assembly: string;
    chrom: string;
    pos: number;
    method: string;
    confidence: "high" | "medium" | "low";
    detail?: string;
    anchors?: string[];
  } | null;
};

type StudyBase = {
  species: string;
  assembly: string;
  trait_text: string;
  window: { mode: "fixed" | "ld"; flank_bp: number; r2?: number };
  top_k_per_locus: number;
  specialists_enabled: SpecialistName[];
};

export type StudyRequest =
  | (StudyBase & { mode: "snps"; snps: SnpInput[] })
  | (StudyBase & { mode: "trait" });

export type StudyValidation = {
  study_normalized: Record<string, unknown> | null;
  placed_snps: SnpInput[];
  warnings: StudyWarning[];
  errors: StudyInputIssue[];
  detail: string;
  preview: { loci: LocusRow[]; genes: number };
};

export type RunSummary = {
  run_id: string;
  status: string;
  created_at: string;
  finished_at: string | null;
};

export type ReportSource = {
  source_id: string;
  name: string;
  version: string;
  license?: string | null;
  retrieved_at?: string | null;
  url?: string | null;
};

export type ReportCitation = {
  alias: string;
  evidence_id: string;
  gene_id: string;
  source_db: string;
  category: string;
  subtype: string;
  quote?: string | null;
  verifier_status?: string | null;
};

export type ClaimVerdict = {
  claim_id: string;
  gene_id: string;
  text: string;
  evidence_ids: string[];
  produced_by: string;
  status: "verified" | "unverified" | "contradicted";
  independent_evidence_ids: string[];
  note: string;
};

export type StudyReport = {
  title: string;
  species: string;
  assembly: string;
  trait: string;
  mode: "snps" | "trait";
  loci: LocusRow[];
  candidates: CandidateRow[];
  candidates_full?: CandidateRow[];
  verification?: ClaimVerdict[];
  citations?: ReportCitation[];
  warnings: StudyWarning[];
  limitations: string[];
  suggested_validations?: string[];
  sources?: ReportSource[];
  stability?: {
    flanks_bp?: number[];
    top_k?: number | null;
    loci?: unknown[];
  };
  evidence_count: number;
  finding_count: number;
  markdown?: string;
  provenance: Record<string, unknown>;
};

export type StudyMetadata = {
  kind: "study";
  /** Read by the server to pick the graph; the SDK drops ``graph_id`` from submit metadata. */
  assistant_id: string;
  graph_id: string;
  species: string;
  assembly: string;
  trait: string;
  n_snps: number;
  mode?: "snps" | "trait";
};

export const TERMINAL_RUN_STATUSES = new Set([
  "completed",
  "failed",
  "cancelled",
  "interrupted",
]);

export class ApiError extends Error {
  status: number;

  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function request<T>(
  apiKey: string,
  path: string,
  init: RequestInit = {},
): Promise<T> {
  const response = await fetch(`${API_URL}${path}`, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(apiKey ? { "X-Api-Key": apiKey } : {}),
      ...init.headers,
    },
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = (await response.json()) as { detail?: unknown };
      if (typeof body.detail === "string") {
        detail = body.detail;
      }
    } catch {
      detail = response.statusText;
    }
    throw new ApiError(response.status, detail || `HTTP ${response.status}`);
  }
  return (await response.json()) as T;
}

export function useStudyApi() {
  const { apiKey } = useApiKey();
  const call = useCallback(
    <T>(path: string, init?: RequestInit) => request<T>(apiKey, path, init),
    [apiKey],
  );
  return useMemo(
    () => ({
      apiKey,
      species: () => call<SpeciesInfo[]>("/registry/species"),
      validate: (study: unknown, signal?: AbortSignal) =>
        call<StudyValidation>("/studies/validate", {
          method: "POST",
          body: JSON.stringify(study),
          signal,
        }),
      runs: (threadId: string) =>
        call<RunSummary[]>(`/threads/${encodeURIComponent(threadId)}/runs`),
      cancel: (threadId: string, runId: string) =>
        call<{ status: string }>(
          `/threads/${encodeURIComponent(threadId)}/runs/${encodeURIComponent(runId)}/cancel`,
          { method: "POST" },
        ),
      thread: (threadId: string) =>
        call<Thread>(`/threads/${encodeURIComponent(threadId)}`),
      studies: () =>
        call<Thread[]>("/threads/search", {
          method: "POST",
          body: JSON.stringify({ metadata: { kind: "study" }, limit: 100 }),
        }),
    }),
    [apiKey, call],
  );
}

export type StudyApi = ReturnType<typeof useStudyApi>;
