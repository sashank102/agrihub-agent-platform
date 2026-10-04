import { produce, type Draft } from "immer";
import { create } from "zustand";
import { immer } from "zustand/middleware/immer";
import {
  PHASES,
  type AgentFocus,
  type AgentKind,
  type AgentRef,
  type OrchestratorDecisionData,
  type OrchestratorPlanData,
  type Phase,
  type PhaseStatus,
  type RunEvent,
  type SourceDiscoveredData,
  type StudyInputIssue,
  type StudyWarning,
} from "./run-events";

export type LaneStatus =
  "queued" | "running" | "completed" | "failed" | "cancelled";
export type ToolStatus = "running" | "ok" | "error" | "cancelled";
export type TerminalStatus =
  "completed" | "failed" | "cancelled" | "interrupted";

export type PhaseState = {
  status: PhaseStatus;
  detail: string | null;
  warnings: StudyWarning[];
  errors: StudyInputIssue[];
  startedAt: string | null;
  endedAt: string | null;
  ts: string;
};

export type DecisionState = OrchestratorDecisionData & {
  eventId: string;
  ts: string;
};

export type AgentState = {
  id: string;
  name: string;
  kind: AgentKind;
  label: string | null;
  parentId: string | null;
  specialist: string | null;
  status: LaneStatus;
  focus: AgentFocus | null;
  maxSteps: number | null;
  step: number;
  stepTitle: string | null;
  stepTs: string | null;
  firstTs: string;
  orderTs: string;
  orderIndex: number;
  startedAt: string | null;
  endedAt: string | null;
  durationMs: number | null;
  summary: string | null;
  findings: number | null;
  model: string | null;
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens: number;
  usageTs: string | null;
  toolIds: string[];
  dispatchEventId: string | null;
  named: boolean;
};

export type ToolState = {
  id: string;
  agentId: string;
  name: string;
  status: ToolStatus;
  inputSummary: string | null;
  outputSummary: string | null;
  evidenceIds: string[];
  outputRef: string | null;
  firstTs: string;
  startedAt: string | null;
  endedAt: string | null;
};

export type HarvestCategory = {
  category: string;
  done: number;
  total: number;
  counts: Record<string, number>;
  ts: string;
};

export type ArtifactState = {
  kind: string;
  title: string;
  artifactId: string | null;
  rows: Record<string, unknown>[] | null;
  ts: string;
  eventId: string;
};

export type OrchestratorUsage = {
  model: string;
  inputTokens: number;
  outputTokens: number;
  cachedInputTokens: number;
  ts: string;
};

export type RunState = {
  threadId: string | null;
  runId: string | null;
  phases: Partial<Record<Phase, PhaseState>>;
  phase: Phase | null;
  plan: (OrchestratorPlanData & { ts: string }) | null;
  decisions: DecisionState[];
  orchestratorUsage: OrchestratorUsage | null;
  agents: Record<string, AgentState>;
  agentOrder: string[];
  tools: Record<string, ToolState>;
  sources: Record<string, SourceDiscoveredData>;
  artifacts: Record<string, ArtifactState>;
  harvest: Record<string, HarvestCategory>;
  evidenceCounts: Record<string, number>;
  evidenceCountsTs: string | null;
  seen: Record<string, true>;
  terminal: { status: TerminalStatus; message: string | null } | null;
  firstTs: string | null;
  lastTs: string | null;
};

const LANE_KINDS = new Set<AgentKind>([
  "specialist",
  "verifier",
  "writer",
  "model",
]);
const PHASE_RANK: Record<PhaseStatus, number> = {
  started: 1,
  completed: 2,
  skipped: 2,
  failed: 2,
};
const LANE_RANK: Record<LaneStatus, number> = {
  queued: 0,
  running: 1,
  completed: 2,
  failed: 2,
  cancelled: 2,
};

export function initialRunState(
  threadId: string | null = null,
  runId: string | null = null,
): RunState {
  return {
    threadId,
    runId,
    phases: {},
    phase: null,
    plan: null,
    decisions: [],
    orchestratorUsage: null,
    agents: {},
    agentOrder: [],
    tools: {},
    sources: {},
    artifacts: {},
    harvest: {},
    evidenceCounts: {},
    evidenceCountsTs: null,
    seen: {},
    terminal: null,
    firstTs: null,
    lastTs: null,
  };
}

export function compareTs(a: string, b: string): number {
  const delta = Date.parse(a) - Date.parse(b);
  if (delta !== 0 && !Number.isNaN(delta)) {
    return delta;
  }
  return a < b ? -1 : a > b ? 1 : 0;
}

function compareEvents(
  a: { ts: string; eventId: string },
  b: { ts: string; eventId: string },
): number {
  return (
    compareTs(a.ts, b.ts) ||
    (a.eventId < b.eventId ? -1 : a.eventId > b.eventId ? 1 : 0)
  );
}

function minTs(current: string | null, next: string): string {
  return current === null || compareTs(next, current) < 0 ? next : current;
}

function maxTs(current: string | null, next: string): string {
  return current === null || compareTs(next, current) > 0 ? next : current;
}

const SPECIALIST_NAMES: Record<string, string> = {
  locus_variant: "Locus and variant specialist",
  qtl_gwas: "QTL and GWAS specialist",
  function_orthology: "Function and orthology specialist",
  expression_network: "Expression and network specialist",
  literature: "Literature specialist",
};

export function specialistLabel(specialist: string): string {
  const known = SPECIALIST_NAMES[specialist];
  if (known) {
    return known;
  }
  const words = specialist.replace(/_/g, " ");
  return `${words.charAt(0).toUpperCase()}${words.slice(1)} specialist`;
}

export function currentPhase(
  phases: Partial<Record<Phase, PhaseState>>,
): Phase | null {
  let running: Phase | null = null;
  let last: Phase | null = null;
  for (const name of PHASES) {
    const state = phases[name];
    if (!state) {
      continue;
    }
    last = name;
    if (state.status === "started") {
      running = name;
    }
  }
  return running ?? last;
}

function ensureLane(
  draft: Draft<RunState>,
  id: string,
  ts: string,
  ref?: AgentRef,
): Draft<AgentState> {
  let lane = draft.agents[id];
  if (!lane) {
    lane = {
      id,
      name: ref?.name ?? id,
      kind: ref?.kind ?? "specialist",
      label: ref?.label ?? null,
      parentId: ref?.parent_id ?? null,
      specialist: null,
      status: "queued",
      focus: null,
      maxSteps: null,
      step: 0,
      stepTitle: null,
      stepTs: null,
      firstTs: ts,
      orderTs: ts,
      orderIndex: 0,
      startedAt: null,
      endedAt: null,
      durationMs: null,
      summary: null,
      findings: null,
      model: null,
      inputTokens: 0,
      outputTokens: 0,
      cachedInputTokens: 0,
      usageTs: null,
      toolIds: [],
      dispatchEventId: null,
      named: ref !== undefined,
    };
    draft.agents[id] = lane;
    draft.agentOrder.push(id);
  } else {
    lane.firstTs = minTs(lane.firstTs, ts);
    if (ref) {
      lane.name = ref.name;
      lane.kind = ref.kind;
      lane.label = ref.label ?? lane.label;
      lane.parentId = ref.parent_id ?? lane.parentId;
      lane.named = true;
    }
  }
  if (compareTs(ts, lane.orderTs) < 0 && lane.dispatchEventId === null) {
    lane.orderTs = ts;
  }
  return lane;
}

function sortLanes(draft: Draft<RunState>): void {
  draft.agentOrder.sort((a, b) => {
    const left = draft.agents[a];
    const right = draft.agents[b];
    return (
      compareTs(left.orderTs, right.orderTs) ||
      left.orderIndex - right.orderIndex ||
      (a < b ? -1 : a > b ? 1 : 0)
    );
  });
}

function markRunning(lane: Draft<AgentState>): void {
  if (LANE_RANK[lane.status] < LANE_RANK.running) {
    lane.status = "running";
  }
}

function ensureTool(
  draft: Draft<RunState>,
  lane: Draft<AgentState>,
  id: string,
  name: string,
  ts: string,
): Draft<ToolState> {
  let tool = draft.tools[id];
  if (!tool) {
    tool = {
      id,
      agentId: lane.id,
      name,
      status: "running",
      inputSummary: null,
      outputSummary: null,
      evidenceIds: [],
      outputRef: null,
      firstTs: ts,
      startedAt: null,
      endedAt: null,
    };
    draft.tools[id] = tool;
    lane.toolIds.push(id);
  } else {
    tool.firstTs = minTs(tool.firstTs, ts);
  }
  return tool;
}

function sortTools(draft: Draft<RunState>, lane: Draft<AgentState>): void {
  lane.toolIds.sort((a, b) => {
    const left = draft.tools[a];
    const right = draft.tools[b];
    return (
      compareTs(
        left.startedAt ?? left.firstTs,
        right.startedAt ?? right.firstTs,
      ) || (a < b ? -1 : a > b ? 1 : 0)
    );
  });
}

function applyPhase(draft: Draft<RunState>, event: RunEvent<"run.phase">) {
  const { phase, status, detail, warnings, errors } = event.data;
  const rank = PHASE_RANK[status];
  const previous = draft.phases[phase];
  if (!previous) {
    draft.phases[phase] = {
      status,
      detail: detail ?? null,
      warnings: warnings ?? [],
      errors: errors ?? [],
      startedAt: status === "started" ? event.ts : null,
      endedAt: rank === 2 ? event.ts : null,
      ts: event.ts,
    };
    return;
  }
  const previousRank = PHASE_RANK[previous.status];
  if (status === "started") {
    previous.startedAt = minTs(previous.startedAt, event.ts);
  }
  if (
    rank > previousRank ||
    (rank === previousRank && compareTs(event.ts, previous.ts) >= 0)
  ) {
    previous.status = status;
    previous.ts = event.ts;
    if (rank === 2) {
      previous.endedAt = event.ts;
    }
  }
  if (detail !== undefined && rank >= previousRank) {
    previous.detail = detail;
  }
  if (warnings?.length) {
    previous.warnings = warnings;
  }
  if (errors?.length) {
    previous.errors = errors;
  }
}

function applyDecision(
  draft: Draft<RunState>,
  event: RunEvent<"orchestrator.decision">,
) {
  draft.decisions.push({
    ...event.data,
    eventId: event.event_id,
    ts: event.ts,
  });
  draft.decisions.sort((a, b) => compareEvents(a, b));
  event.data.dispatched.forEach((dispatch, index) => {
    const lane = ensureLane(draft, dispatch.agent_id, event.ts);
    lane.specialist = dispatch.specialist;
    if (!lane.named) {
      lane.name = specialistLabel(dispatch.specialist);
    }
    const earlier =
      lane.dispatchEventId === null || compareTs(event.ts, lane.orderTs) < 0;
    if (earlier) {
      lane.dispatchEventId = event.event_id;
      lane.orderTs = event.ts;
      lane.orderIndex = index;
    }
  });
  sortLanes(draft);
}

function applyAgentEvent(draft: Draft<RunState>, event: RunEvent) {
  const lane = ensureLane(draft, event.agent.id, event.ts, event.agent);
  switch (event.type) {
    case "agent.started":
      lane.focus = event.data.focus;
      lane.maxSteps = event.data.max_steps;
      lane.startedAt = minTs(lane.startedAt, event.ts);
      markRunning(lane);
      break;
    case "agent.step":
      markRunning(lane);
      lane.maxSteps = event.data.max_steps;
      if (
        event.data.step > lane.step ||
        (event.data.step === lane.step &&
          (lane.stepTs === null || compareTs(event.ts, lane.stepTs) >= 0))
      ) {
        lane.step = event.data.step;
        lane.stepTitle = event.data.title;
        lane.stepTs = event.ts;
      }
      break;
    case "agent.usage":
      if (lane.usageTs === null || compareTs(event.ts, lane.usageTs) >= 0) {
        lane.model = event.data.model;
        lane.inputTokens = event.data.input_tokens;
        lane.outputTokens = event.data.output_tokens;
        lane.cachedInputTokens = event.data.cached_input_tokens ?? 0;
        lane.usageTs = event.ts;
      }
      break;
    case "agent.completed":
      lane.status = event.data.status;
      lane.endedAt = event.ts;
      lane.durationMs = event.data.duration_ms;
      lane.summary = event.data.summary;
      lane.findings = event.data.findings;
      break;
    case "tool.started": {
      markRunning(lane);
      const tool = ensureTool(
        draft,
        lane,
        event.data.tool_call_id,
        event.data.name,
        event.ts,
      );
      tool.inputSummary = event.data.input_summary;
      tool.startedAt = minTs(tool.startedAt, event.ts);
      sortTools(draft, lane);
      break;
    }
    case "tool.finished": {
      markRunning(lane);
      const tool = ensureTool(
        draft,
        lane,
        event.data.tool_call_id,
        event.data.name,
        event.ts,
      );
      tool.status = event.data.status;
      tool.outputSummary = event.data.output_summary;
      tool.evidenceIds = event.data.evidence_ids;
      tool.outputRef = event.data.output_ref;
      tool.endedAt = maxTs(tool.endedAt, event.ts);
      sortTools(draft, lane);
      break;
    }
    default:
      break;
  }
  sortLanes(draft);
}

function applyToDraft(draft: Draft<RunState>, event: RunEvent): void {
  if (draft.seen[event.event_id]) {
    return;
  }
  draft.seen[event.event_id] = true;
  draft.firstTs = minTs(draft.firstTs, event.ts);
  draft.lastTs = maxTs(draft.lastTs, event.ts);
  switch (event.type) {
    case "run.phase":
      applyPhase(draft, event);
      draft.phase = currentPhase(draft.phases);
      return;
    case "orchestrator.plan":
      if (!draft.plan || compareTs(event.ts, draft.plan.ts) >= 0) {
        draft.plan = { ...event.data, ts: event.ts };
      }
      return;
    case "orchestrator.decision":
      applyDecision(draft, event);
      return;
    case "agent.usage":
      if (event.agent.kind === "orchestrator") {
        const previous = draft.orchestratorUsage;
        if (!previous || compareTs(event.ts, previous.ts) >= 0) {
          draft.orchestratorUsage = {
            model: event.data.model,
            inputTokens: event.data.input_tokens,
            outputTokens: event.data.output_tokens,
            cachedInputTokens: event.data.cached_input_tokens ?? 0,
            ts: event.ts,
          };
        }
        return;
      }
      if (LANE_KINDS.has(event.agent.kind)) {
        applyAgentEvent(draft, event);
      }
      return;
    case "source.discovered":
      draft.sources[event.data.source_id] = event.data;
      return;
    case "evidence.progress": {
      const key = event.data.category ?? "all";
      const existing = draft.harvest[key];
      if (
        !existing ||
        event.data.done > existing.done ||
        (event.data.done === existing.done &&
          compareTs(event.ts, existing.ts) >= 0)
      ) {
        draft.harvest[key] = {
          category: key,
          done: event.data.done,
          total: event.data.total,
          counts: event.data.counts,
          ts: event.ts,
        };
      }
      if (
        draft.evidenceCountsTs === null ||
        compareTs(event.ts, draft.evidenceCountsTs) >= 0
      ) {
        draft.evidenceCounts = event.data.counts;
        draft.evidenceCountsTs = event.ts;
      }
      return;
    }
    case "artifact.created": {
      const existing = draft.artifacts[event.data.kind];
      const incoming = { ts: event.ts, eventId: event.event_id };
      if (!existing || compareEvents(incoming, existing) >= 0) {
        draft.artifacts[event.data.kind] = {
          kind: event.data.kind,
          title: event.data.title,
          artifactId: event.data.artifact_id,
          rows: event.data.rows ?? null,
          ...incoming,
        };
      }
      return;
    }
    default:
      if (LANE_KINDS.has(event.agent.kind)) {
        applyAgentEvent(draft, event);
      }
  }
}

function terminateDraft(
  draft: Draft<RunState>,
  status: TerminalStatus,
  message: string | null,
): void {
  draft.terminal = { status, message };
  const failed = status === "failed";
  for (const id of draft.agentOrder) {
    const lane = draft.agents[id];
    if (LANE_RANK[lane.status] < 2) {
      lane.status = failed ? "failed" : "cancelled";
    }
  }
  for (const tool of Object.values(draft.tools)) {
    if (tool.status === "running") {
      tool.status = failed ? "error" : "cancelled";
    }
  }
  if (failed) {
    for (const name of PHASES) {
      const phase = draft.phases[name];
      if (phase?.status === "started") {
        phase.status = "failed";
      }
    }
    draft.phase = currentPhase(draft.phases);
  }
}

export function applyRunEvent(state: RunState, event: RunEvent): RunState {
  return produce(state, (draft) => applyToDraft(draft, event));
}

export function applyRunEvents(
  state: RunState,
  events: readonly RunEvent[],
): RunState {
  return produce(state, (draft) => {
    for (const event of events) {
      applyToDraft(draft, event);
    }
  });
}

export function terminateRun(
  state: RunState,
  status: TerminalStatus,
  message: string | null = null,
): RunState {
  return produce(state, (draft) => terminateDraft(draft, status, message));
}

export function selectLanes(state: RunState): AgentState[] {
  return state.agentOrder.map((id) => state.agents[id]);
}

export function harvestProgress(
  state: Pick<RunState, "harvest"> & {
    phases: Pick<RunState["phases"], "harvest">;
  },
): {
  done: number;
  total: number;
  ratio: number;
} {
  let done = 0;
  let total = 0;
  for (const item of Object.values(state.harvest)) {
    done += item.done;
    total += item.total;
  }
  const finished = state.phases.harvest?.status === "completed";
  const ratio = total > 0 ? done / total : finished ? 1 : 0;
  return { done, total, ratio };
}

export function tokenTotals(
  state: Pick<RunState, "agents"> &
    Partial<Pick<RunState, "orchestratorUsage">>,
): {
  input: number;
  output: number;
  cached: number;
} {
  let input = state.orchestratorUsage?.inputTokens ?? 0;
  let output = state.orchestratorUsage?.outputTokens ?? 0;
  let cached = state.orchestratorUsage?.cachedInputTokens ?? 0;
  for (const lane of Object.values(state.agents)) {
    input += lane.inputTokens;
    output += lane.outputTokens;
    cached += lane.cachedInputTokens;
  }
  return { input, output, cached };
}

export function isRunFinished(state: RunState): boolean {
  return (
    state.terminal !== null ||
    state.artifacts.report !== undefined ||
    state.phases.reporting?.status === "completed"
  );
}

type RunActions = {
  attach: (threadId: string, runId: string | null) => void;
  enqueue: (event: RunEvent, threadId?: string | null) => void;
  applyMany: (events: readonly RunEvent[]) => void;
  flush: () => void;
  terminate: (status: TerminalStatus, message?: string | null) => void;
  reset: () => void;
};

export type RunStore = RunState & RunActions;

let pending: RunEvent[] = [];
let scheduled = false;

function schedule(flush: () => void): void {
  if (scheduled) {
    return;
  }
  scheduled = true;
  if (typeof requestAnimationFrame === "function") {
    requestAnimationFrame(flush);
  } else {
    setTimeout(flush, 16);
  }
}

export const useRunStore = create<RunStore>()(
  immer((set, get) => ({
    ...initialRunState(),
    attach: (threadId, runId) => {
      const state = get();
      if (state.threadId === threadId && state.runId === runId) {
        return;
      }
      pending = [];
      if (state.threadId === threadId && state.runId === null) {
        set((draft) => {
          draft.runId = runId;
        });
        return;
      }
      set(() => initialRunState(threadId, runId));
    },
    enqueue: (event, threadId) => {
      const current = get().threadId;
      if (threadId && current && threadId !== current) {
        return;
      }
      pending.push(event);
      schedule(get().flush);
    },
    applyMany: (events) => {
      set((draft) => {
        for (const event of events) {
          applyToDraft(draft, event);
        }
      });
    },
    flush: () => {
      scheduled = false;
      if (pending.length === 0) {
        return;
      }
      const batch = pending;
      pending = [];
      get().applyMany(batch);
    },
    terminate: (status, message = null) => {
      get().flush();
      set((draft) => terminateDraft(draft, status, message));
    },
    reset: () => {
      pending = [];
      set(() => initialRunState());
    },
  })),
);
