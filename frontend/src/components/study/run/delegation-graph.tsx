"use client";

import { Graph, layout } from "@dagrejs/dagre";
import {
  Background,
  Controls,
  MarkerType,
  ReactFlow,
  type Edge,
  type Node,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { useReducedMotion } from "framer-motion";
import { useMemo } from "react";
import { useShallow } from "zustand/react/shallow";
import type { PhaseState } from "@/lib/run-store";
import { useRunStore, type LaneStatus } from "@/lib/run-store";
import { useRunUi } from "./run-ui-store";

type NodeStatus = LaneStatus | "pending";

const NODE_WIDTH = 210;
const NODE_HEIGHT = 56;

const COLORS: Record<NodeStatus, string> = {
  pending: "#e5e7eb",
  queued: "#e5e7eb",
  running: "#0284c7",
  completed: "#059669",
  failed: "#e11d48",
  cancelled: "#d97706",
};

function phaseStatus(
  phase: PhaseState | undefined,
  stopped: boolean,
): NodeStatus {
  if (!phase) return "pending";
  if (phase.status === "completed" || phase.status === "skipped")
    return "completed";
  if (phase.status === "failed") return "failed";
  return stopped ? "cancelled" : "running";
}

export function DelegationGraph() {
  const { agents, order, phases, terminal } = useRunStore(
    useShallow((state) => ({
      agents: state.agents,
      order: state.agentOrder,
      phases: state.phases,
      terminal: state.terminal,
    })),
  );
  const highlighted = useRunUi((state) => state.highlighted);
  const reduceMotion = useReducedMotion();

  const { nodes, edges } = useMemo(() => {
    const stopped = terminal !== null && terminal.status !== "completed";
    const items: {
      id: string;
      label: string;
      detail: string;
      status: NodeStatus;
    }[] = [
      {
        id: "orchestrator",
        label: "Orchestrator",
        detail: "plan and dispatch",
        status: phaseStatus(phases.planning, stopped),
      },
      ...order.map((id) => ({
        id,
        label: agents[id].name,
        detail: `${agents[id].label ?? ""} · step ${agents[id].step}/${agents[id].maxSteps ?? "?"}`,
        status: agents[id].status as NodeStatus,
      })),
      {
        id: "collect",
        label: "Collect",
        detail: "join findings",
        status: phaseStatus(phases.specialists, stopped),
      },
      {
        id: "ranker",
        label: "Rank and verify",
        detail: "rubric scoring",
        status: phaseStatus(phases.ranking, stopped),
      },
      {
        id: "writer",
        label: "Writer",
        detail: "structured report",
        status: phaseStatus(phases.reporting, stopped),
      },
    ];
    const links: [string, string][] = [
      ...order.map((id) => ["orchestrator", id] as [string, string]),
      ...order.map((id) => [id, "collect"] as [string, string]),
      ["collect", "ranker"],
      ["ranker", "writer"],
    ];
    if (order.length === 0) {
      links.unshift(["orchestrator", "collect"]);
    }
    const graph = new Graph();
    graph.setGraph({ rankdir: "LR", nodesep: 18, ranksep: 70 });
    graph.setDefaultEdgeLabel(() => ({}));
    for (const item of items) {
      graph.setNode(item.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
    }
    for (const [source, target] of links) {
      graph.setEdge(source, target);
    }
    layout(graph);
    const status = new Map(items.map((item) => [item.id, item.status]));
    const nodes: Node[] = items.map((item) => {
      const position = graph.node(item.id) as { x: number; y: number };
      const lit = highlighted.includes(item.id);
      return {
        id: item.id,
        position: {
          x: position.x - NODE_WIDTH / 2,
          y: position.y - NODE_HEIGHT / 2,
        },
        data: {
          label: (
            <div className="flex flex-col text-left">
              <span className="truncate text-xs font-semibold">
                {item.label}
              </span>
              <span className="text-muted-foreground truncate text-[10px]">
                {item.detail}
              </span>
            </div>
          ),
        },
        draggable: false,
        connectable: false,
        style: {
          width: NODE_WIDTH,
          borderRadius: 10,
          borderWidth: lit ? 3 : 2,
          borderColor: lit ? "#38bdf8" : COLORS[item.status],
          padding: 8,
        },
        ariaLabel: `${item.label}: ${item.status}`,
      };
    });
    const edges: Edge[] = links.map(([source, target]) => {
      const running = status.get(target) === "running";
      return {
        id: `${source}-${target}`,
        source,
        target,
        animated: running && !reduceMotion,
        markerEnd: { type: MarkerType.ArrowClosed },
        style: {
          stroke: running ? COLORS.running : "#9ca3af",
          strokeWidth: running ? 2 : 1,
        },
      };
    });
    return { nodes, edges };
  }, [agents, order, phases, terminal, highlighted, reduceMotion]);

  return (
    <div
      className="bg-background h-[520px] rounded-xl border"
      data-testid="delegation-graph"
    >
      <ReactFlow
        nodes={nodes}
        edges={edges}
        fitView
        nodesDraggable={false}
        nodesConnectable={false}
        proOptions={{ hideAttribution: true }}
      >
        <Background />
        <Controls showInteractive={false} />
      </ReactFlow>
    </div>
  );
}
