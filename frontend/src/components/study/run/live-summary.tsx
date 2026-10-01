"use client";

import { useEffect, useRef, useState } from "react";
import { harvestProgress, selectLanes, useRunStore } from "@/lib/run-store";

const PHASE_LABELS: Record<string, string> = {
  intake: "intake",
  model: "model step",
  loci: "building loci",
  harvest: "harvesting evidence",
  planning: "planning",
  specialists: "specialists working",
  ranking: "ranking candidates",
  reporting: "writing the report",
};
const THROTTLE_MS = 4000;

function describe(): string {
  const state = useRunStore.getState();
  const lanes = selectLanes(state);
  const done = lanes.filter(
    (lane) => lane.status !== "running" && lane.status !== "queued",
  ).length;
  const parts = [
    state.terminal
      ? `Study ${state.terminal.status}`
      : state.phase
        ? `Phase: ${PHASE_LABELS[state.phase] ?? state.phase}`
        : "Waiting for the study to start",
  ];
  if (state.phases.harvest && state.phases.harvest.status !== "completed") {
    parts.push(`harvest ${Math.round(harvestProgress(state).ratio * 100)}%`);
  }
  if (lanes.length > 0) {
    parts.push(`${done} of ${lanes.length} specialists finished`);
  }
  return `${parts.join("; ")}.`;
}

export function LiveSummary() {
  const [message, setMessage] = useState("");
  const last = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const update = () => {
      timer.current = null;
      last.current = Date.now();
      setMessage(describe());
    };
    const unsubscribe = useRunStore.subscribe(() => {
      if (timer.current) {
        return;
      }
      const wait = Math.max(0, THROTTLE_MS - (Date.now() - last.current));
      timer.current = setTimeout(update, wait);
    });
    return () => {
      unsubscribe();
      if (timer.current) {
        clearTimeout(timer.current);
      }
    };
  }, []);

  return (
    <p
      aria-live="polite"
      aria-atomic="true"
      className="sr-only"
      data-testid="live-summary"
    >
      {message}
    </p>
  );
}
