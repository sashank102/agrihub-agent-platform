"use client";

import { useQueryState } from "nuqs";
import { useCallback, useEffect } from "react";
import { registerTenantReset } from "@/lib/api-key";
import { useThreads } from "@/providers/Thread";

export function TenantSessionReset() {
  const [, setThreadId] = useQueryState("threadId");
  const [, setHistoryOpen] = useQueryState("chatHistoryOpen");
  const [, setHideToolCalls] = useQueryState("hideToolCalls");
  const { setThreads } = useThreads();

  const reset = useCallback(() => {
    void setThreadId(null);
    void setHistoryOpen(null);
    void setHideToolCalls(null);
    setThreads([]);
  }, [setHistoryOpen, setHideToolCalls, setThreadId, setThreads]);

  useEffect(() => {
    registerTenantReset(reset);
    return () => registerTenantReset(() => undefined);
  }, [reset]);

  return null;
}
