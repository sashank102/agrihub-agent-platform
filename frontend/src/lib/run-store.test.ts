import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { afterEach, describe, expect, it } from "vitest";
import { isRunEvent, parseRunEvents, type RunEvent } from "./run-events";
import {
  applyRunEvent,
  applyRunEvents,
  harvestProgress,
  initialRunState,
  selectLanes,
  terminateRun,
  tokenTotals,
  useRunStore,
  type RunState,
  type RunStore,
} from "./run-store";

const here = path.dirname(fileURLToPath(import.meta.url));
const golden = parseRunEvents(
  fs.readFileSync(path.join(here, "__fixtures__", "poster-run.ndjson"), "utf8"),
);

function live(events: readonly RunEvent[]): RunState {
  return events.reduce(applyRunEvent, initialRunState("thread", "run"));
}

function stateOf(store: RunStore): RunState {
  const {
    attach: _attach,
    enqueue: _enqueue,
    applyMany: _applyMany,
    flush: _flush,
    terminate: _terminate,
    reset: _reset,
    ...data
  } = store;
  return data;
}

function shuffled<T>(items: readonly T[], seed: number): T[] {
  const copy = [...items];
  let state = seed;
  for (let index = copy.length - 1; index > 0; index -= 1) {
    state = (state * 1_103_515_245 + 12_345) % 2_147_483_648;
    const other = state % (index + 1);
    [copy[index], copy[other]] = [copy[other], copy[index]];
  }
  return copy;
}

function digest(state: RunState) {
  return {
    phase: state.phase,
    phases: Object.fromEntries(
      Object.entries(state.phases).map(([name, phase]) => [
        name,
        { status: phase.status, detail: phase.detail },
      ]),
    ),
    lanes: selectLanes(state).map((lane) => ({
      name: lane.name,
      specialist: lane.specialist,
      status: lane.status,
      step: `${lane.step}/${lane.maxSteps}`,
      tools: lane.toolIds.length,
      findings: lane.findings,
      model: lane.model,
    })),
    decisions: state.decisions.map((decision) => decision.kind),
    plan: state.plan?.steps.length,
    harvest: Object.values(state.harvest).map(
      (item) => `${item.category} ${item.done}/${item.total}`,
    ),
    evidence: state.evidenceCounts,
    sources: Object.keys(state.sources).length,
    artifacts: Object.values(state.artifacts).map(
      (artifact) => `${artifact.kind}: ${artifact.title}`,
    ),
  };
}

afterEach(() => {
  useRunStore.getState().reset();
});

describe("the golden poster run", () => {
  it("only contains valid run events", () => {
    const lines = fs
      .readFileSync(
        path.join(here, "__fixtures__", "poster-run.ndjson"),
        "utf8",
      )
      .split("\n")
      .filter(Boolean);
    expect(golden.length).toBe(lines.length);
    expect(golden.length).toBeGreaterThan(100);
    expect(lines.every((line) => isRunEvent(JSON.parse(line)))).toBe(true);
    expect(isRunEvent({ schema: "other", type: "run.phase" })).toBe(false);
  });

  it("rebuilds the finished study", () => {
    const state = live(golden);
    const lanes = selectLanes(state);
    expect(lanes).toHaveLength(5);
    expect(lanes.every((lane) => lane.status === "completed")).toBe(true);
    expect(new Set(lanes.map((lane) => lane.specialist)).size).toBe(4);
    expect(lanes.every((lane) => lane.dispatchEventId !== null)).toBe(true);
    expect(state.decisions.map((decision) => decision.kind)).toEqual([
      "dispatch",
      "followup",
      "finish",
    ]);
    expect(state.decisions.map((decision) => decision.round)).toEqual([
      1, 2, 2,
    ]);
    expect(state.orchestratorUsage?.model).toBe("agrihub-fake:poster");
    expect(tokenTotals(state).input).toBeGreaterThan(
      state.orchestratorUsage?.inputTokens ?? 0,
    );
    expect(harvestProgress(state).ratio).toBe(1);
    expect(
      Object.values(state.harvest).every((item) => item.done === item.total),
    ).toBe(true);
    expect(
      Object.values(state.phases).every(
        (phase) => phase.status === "completed",
      ),
    ).toBe(true);
    expect(Object.keys(state.phases)).toEqual([
      "intake",
      "loci",
      "harvest",
      "planning",
      "specialists",
      "ranking",
      "reporting",
    ]);
    expect(state.phase).toBe("reporting");
    const table = state.artifacts.candidates_table;
    expect(table.title).toBe("Ranked candidates");
    expect(table.rows?.[0]).toMatchObject({ rank: 1, tier: "T4" });
    expect(table.rows?.[0]).toHaveProperty("distance_bp");
    expect(state.artifacts.loci_table.rows).toHaveLength(3);
    expect(state.artifacts.report).toBeDefined();
    expect(Object.keys(state.seen)).toHaveLength(golden.length);
    expect(digest(state)).toMatchSnapshot();
  });

  it("dispatches each specialist on its own genes with a rationale", () => {
    const [first, followup] = live(golden).decisions;
    expect(first.dispatched.length).toBeGreaterThanOrEqual(3);
    const focus = first.dispatched.map((item) =>
      [...item.focus_gene_ids].sort().join(","),
    );
    expect(new Set(focus).size).toBe(focus.length);
    expect(
      first.dispatched.every(
        (item) => item.rationale.length > 0 && item.instructions.length > 0,
      ),
    ).toBe(true);
    expect(first.rejected).toContainEqual(
      expect.objectContaining({ specialist: "expression_network" }),
    );
    expect(followup.dispatched).toHaveLength(1);
    expect(followup.dispatched[0].agent_id).toBe("call_literature_r2");
    const state = live(golden);
    expect(state.agents.call_literature_r2.focus?.round).toBe(2);
  });

  it("ignores duplicates and converges for out-of-order delivery", () => {
    const expected = live(golden);
    const noisy = shuffled([...golden, ...golden.slice(0, 40), ...golden], 7);
    expect(live(noisy)).toEqual(expected);
    expect(live(shuffled(golden, 42))).toEqual(expected);
  });

  it("builds a lane from tool events that arrive before agent.started", () => {
    const lane = golden.find((event) => event.type === "agent.started")!;
    const own = golden.filter((event) => event.agent.id === lane.agent.id);
    const reversed = live([...own].reverse());
    const forward = live(own);
    expect(reversed.agents[lane.agent.id]).toEqual(
      forward.agents[lane.agent.id],
    );
    expect(forward.agents[lane.agent.id].status).toBe("completed");
  });

  it("produces the same state for replay and live delivery", () => {
    const expected = live(golden);
    const store = useRunStore.getState();
    store.attach("thread", "run");
    for (let start = 0; start < golden.length; start += 17) {
      golden.slice(start, start + 17).forEach((event) => store.enqueue(event));
      useRunStore.getState().flush();
    }
    expect(stateOf(useRunStore.getState())).toEqual(expected);
    expect(applyRunEvents(initialRunState("thread", "run"), golden)).toEqual(
      expected,
    );
  });
});

describe("terminal events", () => {
  const cut = golden.findIndex(
    (event) =>
      event.type === "tool.started" &&
      golden.slice(0, golden.indexOf(event)).filter((item) => {
        return item.type === "agent.started";
      }).length === 5,
  );
  const midRun = golden.slice(0, cut + 1);

  it("closes running lanes and tools as cancelled", () => {
    const state = terminateRun(live(midRun), "cancelled");
    const lanes = selectLanes(state);
    expect(lanes).toHaveLength(5);
    expect(lanes.some((lane) => lane.status === "cancelled")).toBe(true);
    expect(
      lanes.every((lane) => ["completed", "cancelled"].includes(lane.status)),
    ).toBe(true);
    expect(
      Object.values(state.tools).every((tool) => tool.status !== "running"),
    ).toBe(true);
    expect(state.terminal).toEqual({ status: "cancelled", message: null });
    expect(state.phases.specialists?.status).toBe("started");
  });

  it("fails open lanes and the running phase when the run fails", () => {
    const state = terminateRun(live(midRun), "failed", "boom");
    expect(
      selectLanes(state).every((lane) =>
        ["completed", "failed"].includes(lane.status),
      ),
    ).toBe(true);
    expect(state.phases.specialists?.status).toBe("failed");
    expect(state.phase).toBe("specialists");
  });

  it("keeps a lane completed when its completion arrives after the close", () => {
    const closed = terminateRun(live(midRun), "cancelled");
    const finished = live(golden);
    const late = golden
      .slice(cut + 1)
      .filter((event) => event.type === "agent.completed");
    const state = late.reduce(applyRunEvent, closed);
    for (const event of late) {
      expect(state.agents[event.agent.id].status).toBe(
        finished.agents[event.agent.id].status,
      );
    }
  });

  it("records a failed intake with its errors", () => {
    const failed: RunEvent<"run.phase"> = {
      ...(golden[0] as RunEvent<"run.phase">),
      event_id: "failed-intake",
      ts: "2026-01-01T00:00:01+00:00",
      data: {
        phase: "intake",
        status: "failed",
        detail: "the study request is invalid: snps: too short",
        errors: [{ loc: ["snps"], message: "too short" }],
      },
    };
    const state = terminateRun(live([golden[0], failed]), "failed");
    expect(state.phases.intake).toMatchObject({
      status: "failed",
      errors: [{ loc: ["snps"], message: "too short" }],
    });
    expect(state.phase).toBe("intake");
  });
});

describe("the run store", () => {
  it("drops events from another thread and resets on a new run", () => {
    const store = useRunStore.getState();
    store.attach("thread-a", "run-a");
    store.enqueue(golden[0], "thread-b");
    store.enqueue(golden[0], "thread-a");
    useRunStore.getState().flush();
    expect(Object.keys(useRunStore.getState().seen)).toEqual([
      golden[0].event_id,
    ]);
    useRunStore.getState().attach("thread-a", "run-b");
    expect(useRunStore.getState().seen).toEqual({});
    expect(useRunStore.getState().runId).toBe("run-b");
  });

  it("keeps live events when the run id arrives after them", () => {
    const store = useRunStore.getState();
    store.attach("thread-a", null);
    store.enqueue(golden[0]);
    useRunStore.getState().flush();
    useRunStore.getState().attach("thread-a", "run-a");
    expect(Object.keys(useRunStore.getState().seen)).toHaveLength(1);
  });
});
