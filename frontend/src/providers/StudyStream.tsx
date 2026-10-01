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
import { STUDY_ASSISTANT_ID } from "@/lib/study-api";
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
  const [created, setCreated] = useState<string | null>(null);
  const routeThread =
    typeof params.threadId === "string" ? params.threadId : null;
  const threadId =
    routeThread ?? (pathname === NEW_STUDY_PATH ? created : null);

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
