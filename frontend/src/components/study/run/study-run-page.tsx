"use client";

import type { Thread } from "@langchain/langgraph-sdk";
import {
  AnimatePresence,
  LayoutGroup,
  MotionConfig,
  motion,
} from "framer-motion";
import { ArrowLeft, Network, Rows3 } from "lucide-react";
import dynamic from "next/dynamic";
import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { toast } from "sonner";
import { Button } from "@/components/ui/button";
import {
  ResizableHandle,
  ResizablePanel,
  ResizablePanelGroup,
} from "@/components/ui/resizable";
import { Skeleton } from "@/components/ui/skeleton";
import { Tabs, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { useMediaQuery } from "@/hooks/useMediaQuery";
import { isUnauthorizedStatus, useApiKey } from "@/lib/api-key";
import type { CandidateRow } from "@/lib/run-events";
import { useRunStore, type TerminalStatus } from "@/lib/run-store";
import {
  ApiError,
  TERMINAL_RUN_STATUSES,
  useStudyApi,
  type RunSummary,
} from "@/lib/study-api";
import { useStreamContext } from "@/providers/Stream";
import { AgentLanes } from "./agent-lanes";
import { CandidatesTable } from "./candidates-table";
import { HarvestLane } from "./harvest-lane";
import { LiveSummary } from "./live-summary";
import { OrchestratorPanel } from "./orchestrator-panel";
import { PhaseStepper } from "./phase-stepper";
import { ResearchTrace } from "./research-trace";
import { RunHeader, type StudyChips } from "./run-header";

const DelegationGraph = dynamic(
  () => import("./delegation-graph").then((module) => module.DelegationGraph),
  { ssr: false, loading: () => <Skeleton className="h-[520px] rounded-xl" /> },
);

const MAX_REJOINS = 10;

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

type StudyValues = {
  species?: string;
  assembly?: string;
  trait_text?: string;
  mode?: string;
  window?: { flank_bp?: number };
  snps?: unknown[];
};

function studyChips(values: unknown, thread: Thread | null): StudyChips {
  const study = (values as { study?: StudyValues } | null)?.study ?? {};
  const metadata = (thread?.metadata ?? {}) as Record<string, unknown>;
  const flank = study.window?.flank_bp;
  return {
    species: study.species ?? (metadata.species as string | undefined),
    assembly: study.assembly ?? (metadata.assembly as string | undefined),
    trait: study.trait_text ?? (metadata.trait as string | undefined),
    windowKb: typeof flank === "number" ? flank / 1000 : undefined,
    snps:
      study.snps?.length ??
      (typeof metadata.n_snps === "number" ? metadata.n_snps : undefined),
    mode: study.mode ?? (metadata.mode as string | undefined),
  };
}

export function StudyRunPage({ threadId }: { threadId: string }) {
  const api = useStudyApi();
  const stream = useStreamContext();
  const { clearApiKey } = useApiKey();
  const wide = useMediaQuery("(min-width: 1024px)");
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [thread, setThread] = useState<Thread | null>(null);
  const [missing, setMissing] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [view, setView] = useState<"lanes" | "graph">("lanes");
  const [traceOpen, setTraceOpen] = useState(false);

  const streamRef = useRef(stream);
  const following = useRef<string | null>(null);
  const joining = useRef(false);
  const active = useRef(true);
  const wasLoading = useRef(stream.isLoading);

  const runId = useRunStore((state) =>
    state.threadId === threadId ? state.runId : null,
  );
  const terminal = useRunStore((state) => state.terminal);
  const hasReport = useRunStore(
    (state) => state.artifacts.report !== undefined,
  );
  const hasLanes = useRunStore((state) => state.agentOrder.length > 0);
  const lastTs = useRunStore((state) => state.lastTs);
  const fallbackRows = useRunStore(
    (state) =>
      (state.artifacts.candidates_table?.rows as CandidateRow[] | undefined) ??
      null,
  );

  useEffect(() => {
    streamRef.current = stream;
  });

  const follow = useCallback(
    async (target: string, join: boolean) => {
      let shouldJoin = join;
      for (
        let attempt = 0;
        attempt <= MAX_REJOINS && active.current;
        attempt += 1
      ) {
        if (shouldJoin) {
          joining.current = true;
          try {
            await streamRef.current.joinStream(target, "-1");
          } finally {
            joining.current = false;
          }
        }
        let list: RunSummary[];
        try {
          list = await api.runs(threadId);
        } catch {
          await sleep(1000);
          continue;
        }
        if (!active.current) {
          return;
        }
        setRuns(list);
        const run = list.find((item) => item.run_id === target);
        const store = useRunStore.getState();
        if (!run || store.runId !== target) {
          return;
        }
        if (TERMINAL_RUN_STATUSES.has(run.status)) {
          if (!store.terminal) {
            store.terminate(run.status as TerminalStatus);
          }
          return;
        }
        await sleep(Math.min(500 * (attempt + 1), 4000));
        shouldJoin = true;
      }
    },
    [api, threadId],
  );

  useEffect(() => {
    active.current = true;
    let cancelled = false;
    Promise.all([api.runs(threadId), api.thread(threadId)])
      .then(([list, info]) => {
        if (cancelled) {
          return;
        }
        setRuns(list);
        setThread(info);
        const latest = list[0];
        const store = useRunStore.getState();
        const live =
          store.threadId === threadId &&
          streamRef.current.isLoading &&
          (store.runId === null || store.runId === latest?.run_id);
        if (live) {
          following.current = store.runId ?? latest?.run_id ?? null;
          return;
        }
        if (!latest || following.current === latest.run_id) {
          return;
        }
        store.attach(threadId, latest.run_id);
        following.current = latest.run_id;
        void follow(latest.run_id, true);
      })
      .catch((error: unknown) => {
        if (isUnauthorizedStatus(error)) {
          clearApiKey("rejected");
          return;
        }
        if (error instanceof ApiError && error.status === 404) {
          setMissing(true);
          return;
        }
        toast.error("The study could not be loaded.");
      });
    return () => {
      cancelled = true;
      active.current = false;
    };
  }, [api, clearApiKey, follow, threadId]);

  useEffect(() => {
    const ended = wasLoading.current && !stream.isLoading;
    wasLoading.current = stream.isLoading;
    const target = following.current ?? runId;
    if (ended && target && !joining.current) {
      following.current = target;
      void follow(target, false);
    }
  }, [stream.isLoading, runId, follow]);

  const cancel = async () => {
    if (!runId) {
      return;
    }
    setCancelling(true);
    try {
      const result = await api.cancel(threadId, runId);
      useRunStore
        .getState()
        .terminate(
          TERMINAL_RUN_STATUSES.has(result.status)
            ? (result.status as TerminalStatus)
            : "cancelled",
        );
      setRuns(await api.runs(threadId));
    } catch (error) {
      if (isUnauthorizedStatus(error)) {
        clearApiKey("rejected");
        return;
      }
      toast.error("The run could not be cancelled.");
    } finally {
      setCancelling(false);
    }
  };

  if (missing) {
    return (
      <div className="flex flex-col items-center gap-3 py-16 text-center">
        <h1 className="text-xl font-semibold">Study not found</h1>
        <p className="text-muted-foreground text-sm">
          It does not exist or belongs to another account.
        </p>
        <Button
          asChild
          variant="outline"
        >
          <Link href="/">
            <ArrowLeft />
            All studies
          </Link>
        </Button>
      </div>
    );
  }

  const latest = runs?.find((item) => item.run_id === runId) ?? runs?.[0];
  const running =
    !terminal &&
    (stream.isLoading ||
      latest?.status === "running" ||
      latest?.status === "pending");
  const status =
    terminal?.status ??
    latest?.status ??
    (stream.isLoading ? "running" : "pending");
  const collapsed = hasReport || terminal?.status === "completed";
  const report = stream.values?.report ?? null;

  const workspace = (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-2">
        <h2 className="font-semibold tracking-tight">Agents</h2>
        <Tabs
          value={view}
          onValueChange={(value) => setView(value as "lanes" | "graph")}
          className="ml-auto"
        >
          <TabsList aria-label="Agent view">
            <TabsTrigger value="lanes">
              <Rows3 />
              Lanes
            </TabsTrigger>
            <TabsTrigger value="graph">
              <Network />
              Graph
            </TabsTrigger>
          </TabsList>
        </Tabs>
      </div>
      <HarvestLane />
      {view === "graph" ? <DelegationGraph /> : <AgentLanes />}
    </div>
  );

  return (
    <MotionConfig reducedMotion="user">
      <div
        className="flex flex-col gap-5"
        data-testid="run-view"
        data-collapsed={collapsed || undefined}
      >
        <RunHeader
          chips={studyChips(stream.values, thread)}
          status={status}
          start={latest?.created_at ?? null}
          end={latest?.finished_at ?? (terminal ? lastTs : null)}
          canCancel={running && !!runId}
          cancelling={cancelling}
          onCancel={() => void cancel()}
        />
        <PhaseStepper />
        <LiveSummary />
        {runs !== null && runs.length === 0 && !stream.isLoading && (
          <p className="text-muted-foreground text-sm">
            This study has no runs yet.
          </p>
        )}
        <LayoutGroup>
          <AnimatePresence
            mode="popLayout"
            initial={false}
          >
            {collapsed ? (
              <motion.div
                key="report"
                className="flex flex-col gap-6"
                initial={{ opacity: 0, y: 12 }}
                animate={{ opacity: 1, y: 0 }}
                transition={{ duration: 0.35 }}
              >
                <ResearchTrace
                  open={traceOpen}
                  onOpenChange={setTraceOpen}
                />
                <CandidatesTable
                  report={report}
                  fallbackRows={fallbackRows}
                />
              </motion.div>
            ) : (
              <motion.div
                key="live"
                exit={{ opacity: 0 }}
                transition={{ duration: 0.25 }}
              >
                {wide ? (
                  <ResizablePanelGroup
                    orientation="horizontal"
                    className="items-start gap-0"
                  >
                    <ResizablePanel
                      defaultSize="28%"
                      minSize="18%"
                      maxSize="45%"
                    >
                      <div className="pr-4">
                        <OrchestratorPanel />
                      </div>
                    </ResizablePanel>
                    <ResizableHandle withHandle />
                    <ResizablePanel>
                      <div className="pl-4">{workspace}</div>
                    </ResizablePanel>
                  </ResizablePanelGroup>
                ) : (
                  <div className="flex flex-col gap-6">
                    <OrchestratorPanel />
                    {workspace}
                  </div>
                )}
                {terminal && !hasLanes && terminal.status !== "completed" && (
                  <p className="text-muted-foreground mt-4 text-sm">
                    The study stopped before any specialist was dispatched.
                  </p>
                )}
              </motion.div>
            )}
          </AnimatePresence>
        </LayoutGroup>
      </div>
    </MotionConfig>
  );
}
