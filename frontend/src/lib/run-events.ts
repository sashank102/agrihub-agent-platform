export const RUN_EVENT_SCHEMA = "agrihub.run-event/v1";

export const PHASES = [
  "intake",
  "model",
  "loci",
  "harvest",
  "planning",
  "specialists",
  "ranking",
  "reporting",
] as const;
export type Phase = (typeof PHASES)[number];
export type PhaseStatus = "started" | "completed" | "skipped" | "failed";

export const AGENT_KINDS = [
  "pipeline",
  "orchestrator",
  "specialist",
  "verifier",
  "writer",
  "model",
] as const;
export type AgentKind = (typeof AGENT_KINDS)[number];
export type DecisionKind = "dispatch" | "reflect" | "followup" | "finish";
export type CauseType = "toolCall" | "send" | "edge";

export const SPECIALISTS = [
  "locus_variant",
  "qtl_gwas",
  "function_orthology",
  "expression_network",
  "literature",
] as const;
export type SpecialistName = (typeof SPECIALISTS)[number];

export type AgentRef = {
  id: string;
  name: string;
  kind: AgentKind;
  parent_id: string | null;
  label: string | null;
};

export type Cause = {
  type: CauseType;
  tool_call_id?: string | null;
};

export type StudyWarning = {
  code: string;
  message: string;
  snp?: string | null;
};

export type StudyInputIssue = {
  loc: (string | number)[];
  message: string;
};

export type RunPhaseData = {
  phase: Phase;
  status: PhaseStatus;
  detail?: string;
  warnings?: StudyWarning[];
  errors?: StudyInputIssue[];
};

export type OrchestratorPlanData = {
  summary: string;
  steps: string[];
};

export type Dispatch = {
  agent_id: string;
  specialist: SpecialistName | string;
  focus_gene_ids: string[];
  focus_loci: string[];
};

export type Rejection = {
  specialist: SpecialistName | string;
  reason: string;
};

export type OrchestratorDecisionData = {
  kind: DecisionKind;
  rationale: string;
  dispatched: Dispatch[];
  rejected: Rejection[];
};

export type AgentFocus = {
  gene_ids?: string[];
  loci?: string[];
  instructions?: string;
  [key: string]: unknown;
};

export type AgentStartedData = {
  focus: AgentFocus;
  max_steps: number;
};

export type AgentStepData = {
  step: number;
  max_steps: number;
  title: string;
};

export type AgentUsageData = {
  model: string;
  input_tokens: number;
  output_tokens: number;
};

export type AgentCompletedData = {
  status: "completed" | "failed";
  summary: string;
  findings: number;
  duration_ms: number;
};

export type ToolStartedData = {
  tool_call_id: string;
  name: string;
  input_summary: string;
};

export type ToolFinishedData = {
  tool_call_id: string;
  name: string;
  status: "ok" | "error";
  output_summary: string;
  evidence_ids: string[];
  output_ref: string | null;
};

export type SourceDiscoveredData = {
  source_id: string;
  name: string;
  version: string;
  url: string | null;
  license: string | null;
};

export type EvidenceProgressData = {
  category?: string;
  counts: Record<string, number>;
  done: number;
  total: number;
};

export type ArtifactKind =
  "loci_table" | "candidates_table" | "report" | "evidence_snapshot";

export type LocusRow = {
  locus_id: string;
  lead_snp: string;
  supporting_snps: string[];
  chrom: string;
  start: number;
  end: number;
  assembly: string;
  window_method: "fixed" | "ld";
  merged_from: string[];
  lead_pos: number | null;
  snp_positions: Record<string, number>;
  n_genes: number;
  genes_capped: boolean;
};

export type Tier = "T1" | "T2" | "T3" | "T4";

export type CandidateRow = {
  rank?: number;
  gene_id: string;
  locus_id: string;
  symbol?: string | null;
  rank_in_locus?: number | null;
  score: number;
  tier: Tier;
  stability?: string | null;
  chrom?: string | null;
  start?: number | null;
  end?: number | null;
  strand?: "+" | "-" | ".";
  distance_bp?: number | null;
  overlaps_snp?: boolean;
  nearest_snp?: string | null;
  lead_snp?: string | null;
  defline?: string;
  [key: string]: unknown;
};

export type ArtifactCreatedData = {
  kind: ArtifactKind | (string & {});
  title: string;
  artifact_id: string | null;
  rows?: Record<string, unknown>[];
};

export type RunEventDataMap = {
  "run.phase": RunPhaseData;
  "orchestrator.plan": OrchestratorPlanData;
  "orchestrator.decision": OrchestratorDecisionData;
  "agent.started": AgentStartedData;
  "agent.step": AgentStepData;
  "agent.usage": AgentUsageData;
  "agent.completed": AgentCompletedData;
  "tool.started": ToolStartedData;
  "tool.finished": ToolFinishedData;
  "source.discovered": SourceDiscoveredData;
  "evidence.progress": EvidenceProgressData;
  "artifact.created": ArtifactCreatedData;
};

export type RunEventType = keyof RunEventDataMap;

export const RUN_EVENT_TYPES: readonly RunEventType[] = [
  "run.phase",
  "orchestrator.plan",
  "orchestrator.decision",
  "agent.started",
  "agent.step",
  "agent.usage",
  "agent.completed",
  "tool.started",
  "tool.finished",
  "source.discovered",
  "evidence.progress",
  "artifact.created",
];

type Envelope<T extends RunEventType> = {
  schema: typeof RUN_EVENT_SCHEMA;
  type: T;
  event_id: string;
  ts: string;
  agent: AgentRef;
  cause: Cause;
  ns: string[];
  data: RunEventDataMap[T];
};

export type RunEvent<T extends RunEventType = RunEventType> = {
  [K in T]: Envelope<K>;
}[T];

const EVENT_TYPES = new Set<string>(RUN_EVENT_TYPES);
const KINDS = new Set<string>(AGENT_KINDS);

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function isRunEvent(value: unknown): value is RunEvent {
  if (!isRecord(value) || value.schema !== RUN_EVENT_SCHEMA) {
    return false;
  }
  const agent = value.agent;
  return (
    typeof value.type === "string" &&
    EVENT_TYPES.has(value.type) &&
    typeof value.event_id === "string" &&
    typeof value.ts === "string" &&
    isRecord(agent) &&
    typeof agent.id === "string" &&
    typeof agent.kind === "string" &&
    KINDS.has(agent.kind) &&
    isRecord(value.data)
  );
}

export function parseRunEvents(ndjson: string): RunEvent[] {
  return ndjson
    .split("\n")
    .filter((line) => line.trim())
    .map((line) => JSON.parse(line) as unknown)
    .filter(isRunEvent);
}
