"use client";

import type { Thread } from "@langchain/langgraph-sdk";
import { formatDistanceToNow } from "date-fns";
import { FlaskConical, Plus, RefreshCw } from "lucide-react";
import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { isUnauthorizedStatus, useApiKey } from "@/lib/api-key";
import { useStudyApi, type RunSummary } from "@/lib/study-api";
import { RunStatusBadge } from "./run-status-badge";

type StudyRow = {
  thread: Thread;
  run: RunSummary | null;
};

function meta(thread: Thread, key: string): string | undefined {
  const value = (thread.metadata as Record<string, unknown> | null)?.[key];
  return value === undefined || value === null ? undefined : String(value);
}

function threadStatus(thread: Thread): string {
  if (thread.status === "busy") {
    return "running";
  }
  if (thread.status === "error") {
    return "failed";
  }
  return thread.status;
}

export function StudiesDashboard() {
  const api = useStudyApi();
  const { clearApiKey } = useApiKey();
  const [rows, setRows] = useState<StudyRow[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const threads = await api.studies();
      threads.sort((a, b) => b.created_at.localeCompare(a.created_at));
      const runs = await Promise.allSettled(
        threads.map((thread) => api.runs(thread.thread_id)),
      );
      setRows(
        threads.map((thread, index) => {
          const result = runs[index];
          return {
            thread,
            run:
              result.status === "fulfilled" ? (result.value[0] ?? null) : null,
          };
        }),
      );
      setError(null);
    } catch (failure) {
      if (isUnauthorizedStatus(failure)) {
        clearApiKey("rejected");
        return;
      }
      setError("The study list could not be loaded from the AgriHub server.");
      setRows((current) => current ?? []);
    } finally {
      setRefreshing(false);
    }
  }, [api, clearApiKey]);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Studies</h1>
          <p className="text-muted-foreground text-sm">
            Post-GWAS candidate gene studies run by the deterministic pipeline
            and its specialist agents.
          </p>
        </div>
        <div className="flex gap-2">
          <Button
            variant="outline"
            size="sm"
            onClick={() => {
              setRefreshing(true);
              void load();
            }}
            disabled={refreshing}
          >
            <RefreshCw className={refreshing ? "animate-spin" : undefined} />
            Refresh
          </Button>
          <Button
            asChild
            variant="brand"
          >
            <Link href="/studies/new">
              <Plus />
              New study
            </Link>
          </Button>
        </div>
      </div>
      {error && (
        <p
          role="alert"
          className="text-sm text-rose-600"
        >
          {error}
        </p>
      )}
      {rows === null ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {[0, 1, 2].map((item) => (
            <Skeleton
              key={item}
              className="h-32 rounded-xl"
            />
          ))}
        </div>
      ) : rows.length === 0 ? (
        <div className="bg-background flex flex-col items-center gap-3 rounded-xl border border-dashed p-12 text-center">
          <FlaskConical className="text-muted-foreground size-8" />
          <h2 className="font-medium">No studies yet</h2>
          <p className="text-muted-foreground max-w-md text-sm">
            Start with a species, a trait and a list of significant SNPs. The
            study places them on the assembly, builds loci and ranks the genes
            inside them.
          </p>
          <Button
            asChild
            variant="brand"
          >
            <Link href="/studies/new">
              <Plus />
              New study
            </Link>
          </Button>
        </div>
      ) : (
        <ul
          aria-label="Studies"
          className="grid gap-3 md:grid-cols-2 xl:grid-cols-3"
        >
          {rows.map(({ thread, run }) => {
            const snps = meta(thread, "n_snps");
            return (
              <li key={thread.thread_id}>
                <Link
                  href={`/studies/${thread.thread_id}`}
                  className="bg-background hover:border-foreground/30 focus-visible:ring-ring/50 flex h-full flex-col gap-3 rounded-xl border p-4 shadow-xs transition-colors outline-none focus-visible:ring-[3px]"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0">
                      <p className="truncate font-medium">
                        {meta(thread, "trait") ?? "Untitled study"}
                      </p>
                      <p className="text-muted-foreground text-sm capitalize">
                        {meta(thread, "species") ?? "unknown species"}
                      </p>
                    </div>
                    <RunStatusBadge
                      status={run?.status ?? threadStatus(thread)}
                    />
                  </div>
                  <div className="text-muted-foreground mt-auto flex flex-wrap items-center gap-2 text-xs">
                    {meta(thread, "assembly") && (
                      <Badge variant="outline">
                        {meta(thread, "assembly")}
                      </Badge>
                    )}
                    {snps !== undefined && (
                      <Badge variant="outline">
                        {snps} SNP{snps === "1" ? "" : "s"}
                      </Badge>
                    )}
                    <span className="ml-auto">
                      {formatDistanceToNow(new Date(thread.created_at), {
                        addSuffix: true,
                      })}
                    </span>
                  </div>
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
