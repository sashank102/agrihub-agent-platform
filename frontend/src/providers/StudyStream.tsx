"use client";

import { useParams, usePathname, useRouter } from "next/navigation";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import { toast } from "sonner";
import { registerTenantReset } from "@/lib/api-key";
import { useRunStore } from "@/lib/run-store";
import { ApiError, STUDY_ASSISTANT_ID, useStudyApi } from "@/lib/study-api";
import { Button } from "@/components/ui/button";
import Link from "next/link";
import { AuthenticatedStream, type RunCreated } from "./Stream";

type StudySession = {
  beginNewStudy: () => void;
};

const StudySessionContext = createContext<StudySession | undefined>(undefined);

export const NEW_STUDY_PATH = "/studies/new";

function describeRunError(error: unknown): string {
  if (error instanceof Error && error.message) {
    return error.message;
  }
  if (typeof error === "object" && error !== null && "message" in error) {
    return String(error.message);
  }
  return "The study could not be completed.";
}

export function StudyStreamProvider({ children }: { children: ReactNode }) {
  const params = useParams<{ threadId?: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const api = useStudyApi();
  const [created, setCreated] = useState<string | null>(null);
  const [checked, setChecked] = useState<{
    id: string;
    access: "ok" | "missing";
  } | null>(null);
  const routeThread =
    typeof params.threadId === "string" ? params.threadId : null;
  const threadId =
    routeThread ?? (pathname === NEW_STUDY_PATH ? created : null);
  const mustCheck = Boolean(routeThread && routeThread !== created);
  const access =
    !mustCheck || !routeThread
      ? "ok"
      : checked?.id === routeThread
        ? checked.access
        : "checking";

  useEffect(() => {
    if (!routeThread || routeThread === created) {
      return;
    }
    let cancelled = false;
    api
      .thread(routeThread)
      .then(() => {
        if (!cancelled) {
          setChecked({ id: routeThread, access: "ok" });
        }
      })
      .catch((error: unknown) => {
        if (cancelled) {
          return;
        }
        setChecked({
          id: routeThread,
          access:
            error instanceof ApiError && error.status === 404
              ? "missing"
              : "ok",
        });
      });
    return () => {
      cancelled = true;
    };
  }, [api, created, routeThread]);

  const onCreated = useCallback(
    (run: RunCreated) => {
      useRunStore.getState().attach(run.thread_id, run.run_id);
      router.push(`/studies/${run.thread_id}`);
    },
    [router],
  );
  const onRunError = useCallback((error: unknown) => {
    if (
      typeof error === "object" &&
      error !== null &&
      "status" in error &&
      error.status === 404
    ) {
      return;
    }
    toast.error("The study did not finish", {
      description: describeRunError(error),
    });
  }, []);
  const beginNewStudy = useCallback(() => {
    setCreated(null);
    useRunStore.getState().reset();
  }, []);

  useEffect(() => {
    registerTenantReset(() => {
      setCreated(null);
      useRunStore.getState().reset();
      router.replace("/");
    });
    return () => registerTenantReset(() => undefined);
  }, [router]);

  const session = useMemo(() => ({ beginNewStudy }), [beginNewStudy]);

  if (access === "missing") {
    return (
      <StudySessionContext.Provider value={session}>
        <div className="flex flex-col items-center gap-3 py-16 text-center">
          <h1 className="text-xl font-semibold">Study not found</h1>
          <p className="text-muted-foreground text-sm">
            It does not exist or belongs to another account.
          </p>
          <Button
            asChild
            variant="outline"
          >
            <Link href="/">Back to studies</Link>
          </Button>
        </div>
      </StudySessionContext.Provider>
    );
  }

  if (access === "checking") {
    return (
      <StudySessionContext.Provider value={session}>
        <p className="text-muted-foreground p-6 text-sm">Opening the study…</p>
      </StudySessionContext.Provider>
    );
  }

  return (
    <StudySessionContext.Provider value={session}>
      <AuthenticatedStream
        assistantId={STUDY_ASSISTANT_ID}
        threadId={threadId}
        onThreadId={setCreated}
        onCreated={onCreated}
        onRunError={onRunError}
        fetchStateHistory={false}
      >
        {children}
      </AuthenticatedStream>
    </StudySessionContext.Provider>
  );
}

export function useStudySession(): StudySession {
  const context = useContext(StudySessionContext);
  if (context === undefined) {
    throw new Error("useStudySession must be used within StudyStreamProvider");
  }
  return context;
}
