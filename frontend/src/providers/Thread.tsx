import { validate } from "uuid";
import { isUnauthorizedStatus, useApiKey } from "@/lib/api-key";
import { API_URL, CHAT_ASSISTANT_ID } from "@/lib/study-api";
import { Thread } from "@langchain/langgraph-sdk";
import {
  createContext,
  useContext,
  ReactNode,
  useCallback,
  useState,
  Dispatch,
  SetStateAction,
} from "react";
import { createClient } from "./client";

interface ThreadContextType {
  getThreads: () => Promise<Thread[]>;
  threads: Thread[];
  setThreads: Dispatch<SetStateAction<Thread[]>>;
  threadsLoading: boolean;
  setThreadsLoading: Dispatch<SetStateAction<boolean>>;
}

const ThreadContext = createContext<ThreadContextType | undefined>(undefined);

function getThreadSearchMetadata(
  assistantId: string,
): { graph_id: string } | { assistant_id: string } {
  if (validate(assistantId)) {
    return { assistant_id: assistantId };
  }
  return { graph_id: assistantId };
}

export function ThreadProvider({ children }: { children: ReactNode }) {
  const { apiKey, clearApiKey } = useApiKey();
  const [threads, setThreads] = useState<Thread[]>([]);
  const [threadsLoading, setThreadsLoading] = useState(false);
  const [loadedKey, setLoadedKey] = useState(apiKey);
  if (loadedKey !== apiKey) {
    setLoadedKey(apiKey);
    setThreads([]);
  }

  const getThreads = useCallback(async (): Promise<Thread[]> => {
    if (!apiKey) {
      return [];
    }
    const client = createClient(API_URL, apiKey);
    try {
      return await client.threads.search({
        metadata: {
          ...getThreadSearchMetadata(CHAT_ASSISTANT_ID),
        },
        limit: 100,
      });
    } catch (error) {
      if (isUnauthorizedStatus(error)) {
        clearApiKey("rejected");
        return [];
      }
      throw error;
    }
  }, [apiKey, clearApiKey]);

  const value = {
    getThreads,
    threads,
    setThreads,
    threadsLoading,
    setThreadsLoading,
  };

  return (
    <ThreadContext.Provider value={value}>{children}</ThreadContext.Provider>
  );
}

export function useThreads() {
  const context = useContext(ThreadContext);
  if (context === undefined) {
    throw new Error("useThreads must be used within a ThreadProvider");
  }
  return context;
}
