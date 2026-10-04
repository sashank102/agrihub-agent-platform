"use client";

import { Clock, Coins, Loader2, Square } from "lucide-react";
import { useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { tokenTotals, useRunStore } from "@/lib/run-store";
import { RunStatusBadge } from "../run-status-badge";
import { formatDuration, formatTokens } from "./format";

export type StudyChips = {
  species?: string;
  assembly?: string;
  trait?: string;
  windowKb?: number;
  snps?: number;
  mode?: string;
};

function Elapsed({ start, end }: { start: string | null; end: string | null }) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (end || !start) {
      return;
    }
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [start, end]);
  if (!start) {
    return <span>—</span>;
  }
  const finish = end ? Date.parse(end) : now;
  return <span>{formatDuration(finish - Date.parse(start))}</span>;
}

export function RunHeader({
  chips,
  status,
  start,
  end,
  canCancel,
  cancelling,
  onCancel,
}: {
  chips: StudyChips;
  status: string;
  start: string | null;
  end: string | null;
  canCancel: boolean;
  cancelling: boolean;
  onCancel: () => void;
}) {
  const agents = useRunStore((state) => state.agents);
  const orchestratorUsage = useRunStore((state) => state.orchestratorUsage);
  const tokens = tokenTotals({ agents, orchestratorUsage });
  const stub = Object.values(agents).some((lane) => lane.model === "stub");
  return (
    <header className="flex flex-wrap items-start gap-4">
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <h1 className="truncate text-2xl font-semibold tracking-tight">
            {chips.trait ?? "Study"}
          </h1>
          <RunStatusBadge status={status} />
        </div>
        <ul
          aria-label="Study settings"
          className="flex flex-wrap gap-1.5"
        >
          {chips.species && (
            <li>
              <Badge
                variant="secondary"
                className="capitalize"
              >
                {chips.species}
              </Badge>
            </li>
          )}
          {chips.assembly && (
            <li>
              <Badge variant="secondary">{chips.assembly}</Badge>
            </li>
          )}
          {chips.windowKb !== undefined && (
            <li>
              <Badge variant="secondary">±{chips.windowKb} kb</Badge>
            </li>
          )}
          {chips.mode === "trait" ? (
            <li>
              <Badge variant="secondary">trait mode</Badge>
            </li>
          ) : chips.snps !== undefined ? (
            <li>
              <Badge variant="secondary">
                {chips.snps} SNP{chips.snps === 1 ? "" : "s"}
              </Badge>
            </li>
          ) : null}
        </ul>
      </div>
      <div className="text-muted-foreground flex items-center gap-4 text-sm tabular-nums">
        <span
          className="flex items-center gap-1.5"
          title="Elapsed time"
        >
          <Clock className="size-4" />
          <Elapsed
            start={start}
            end={end}
          />
        </span>
        <span
          className="flex items-center gap-1.5"
          title="Model tokens (input + output); cached input is read from the prompt cache"
        >
          <Coins className="size-4" />
          {formatTokens(tokens.input + tokens.output)} tokens
          {stub ? " (stub models)" : ""}
          {tokens.cached > 0 && ` · ${formatTokens(tokens.cached)} cached`}
        </span>
        {canCancel && (
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={onCancel}
            disabled={cancelling}
          >
            {cancelling ? <Loader2 className="animate-spin" /> : <Square />}
            Cancel
          </Button>
        )}
      </div>
    </header>
  );
}
