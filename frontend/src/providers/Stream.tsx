import React, {
  useContext,
  ReactNode,
  useState,
  useEffect,
  useCallback,
} from "react";
import { useStream } from "@langchain/langgraph-sdk/react";
import { type Message } from "@langchain/langgraph-sdk";
import {
  uiMessageReducer,
  isUIMessage,
  isRemoveUIMessage,
  type UIMessage,
  type RemoveUIMessage,
} from "@langchain/langgraph-sdk/react-ui";
import { useQueryState } from "nuqs";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { ArrowRight, Sprout } from "lucide-react";
import { PasswordInput } from "@/components/ui/password-input";
import { isUnauthorizedStatus, useApiKey } from "@/lib/api-key";
import { useThreads } from "./Thread";
import { toast } from "sonner";

export type StateType = { messages: Message[]; ui?: UIMessage[] };

const useTypedStream = useStream<
  StateType,
  {
    UpdateType: {
      messages?: Message[] | Message | string;
      ui?: (UIMessage | RemoveUIMessage)[] | UIMessage | RemoveUIMessage;
      context?: Record<string, unknown>;
    };
    CustomEventType: UIMessage | RemoveUIMessage;
  }
>;

type StreamContextType = ReturnType<typeof useTypedStream>;
const StreamContext = React.createContext<StreamContextType | undefined>(
  undefined,
);

const API_URL = process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:8000";
const ASSISTANT_ID = process.env.NEXT_PUBLIC_ASSISTANT_ID || "agrihub";

type ProbeResult = "ok" | "unauthorized" | "unreachable" | "server";

async function probeApi(apiUrl: string, apiKey: string): Promise<ProbeResult> {
  try {
    const response = await fetch(`${apiUrl}/threads/search`, {
      method: "POST",
      headers: {
        "X-Api-Key": apiKey,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ limit: 1 }),
    });
    if (response.status === 401) {
      return "unauthorized";
    }
    if (!response.ok) {
      return "server";
    }
    return "ok";
  } catch {
    return "unreachable";
  }
}

function noticeCopy(notice: string | null, connection: string): string | null {
  if (notice === "rejected") {
    return "That API key was rejected. Check that it is active and belongs to an enabled user.";
  }
  if (notice === "unreachable" || connection === "unreachable") {
    return "The AgriHub server could not be reached. This is a connection problem, not a rejected key.";
  }
  if (notice === "server" || connection === "server") {
    return "The AgriHub server returned an error before accepting the key.";
  }
  return null;
}

const StreamSession = ({
  children,
  apiKey,
  apiUrl,
  assistantId,
}: {
  children: ReactNode;
  apiKey: string;
  apiUrl: string;
  assistantId: string;
}) => {
  const [threadId, setThreadId] = useQueryState("threadId");
  const { getThreads, setThreads } = useThreads();
  const { clearApiKey, setConnection } = useApiKey();
  const handleFailure = useCallback(
    (error: unknown) => {
      if (isUnauthorizedStatus(error)) {
        clearApiKey("rejected");
        return;
      }
      toast.error("The AgriHub server could not complete that request.", {
        description: "The API key was not included in this message.",
      });
    },
    [clearApiKey],
  );
  const streamValue = useTypedStream({
    apiUrl,
    apiKey: apiKey || undefined,
    defaultHeaders: apiKey ? { "X-Api-Key": apiKey } : undefined,
    assistantId,
    threadId: threadId ?? null,
    fetchStateHistory: true,
    onError: handleFailure,
    onCustomEvent: (event, options) => {
      if (isUIMessage(event) || isRemoveUIMessage(event)) {
        options.mutate((prev) => {
          const ui = uiMessageReducer(prev.ui ?? [], event);
          return { ...prev, ui };
        });
      }
    },
    onThreadId: (id) => {
      setThreadId(id);
      getThreads().then(setThreads).catch(handleFailure);
    },
  });

  useEffect(() => {
    if (!apiKey) {
      setConnection("ok");
      return;
    }
    let cancelled = false;
    probeApi(apiUrl, apiKey).then((result) => {
      if (cancelled) {
        return;
      }
      if (result === "unauthorized") {
        clearApiKey("rejected");
        return;
      }
      if (result === "unreachable") {
        setConnection("unreachable");
        toast.error("Cannot reach the AgriHub server", {
          description: `No response from ${apiUrl}. The API key was not reported.`,
        });
        return;
      }
      if (result === "server") {
        setConnection("server");
        toast.error("The AgriHub server returned an error", {
          description: "Authentication was not the failure.",
        });
        return;
      }
      setConnection("ok");
    });
    return () => {
      cancelled = true;
    };
  }, [apiKey, apiUrl, clearApiKey, setConnection]);

  return (
    <StreamContext.Provider value={streamValue}>
      {children}
    </StreamContext.Provider>
  );
};

function ApiKeyGate() {
  const { notice, connection, saveApiKey, enterDevelopmentMode } = useApiKey();
  const [remember, setRemember] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [localNotice, setLocalNotice] = useState<string | null>(null);
  const [authMode, setAuthMode] = useState<"unknown" | "api_key" | "disabled">(
    "unknown",
  );
  const message = localNotice ?? noticeCopy(notice, connection);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_URL}/info`)
      .then(async (response) => {
        if (!response.ok) {
          return "unknown" as const;
        }
        const body = (await response.json()) as { auth_mode?: string };
        return body.auth_mode === "disabled" ? "disabled" : "api_key";
      })
      .then((mode) => {
        if (!cancelled) {
          setAuthMode(mode);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setAuthMode("unknown");
        }
      });
    return () => {
      cancelled = true;
    };
  }, []);

  if (authMode === "disabled") {
    return (
      <div className="flex min-h-screen w-full items-center justify-center p-4">
        <div className="bg-background flex w-full max-w-xl flex-col gap-4 rounded-lg border p-6 shadow-lg">
          <Sprout className="size-7" />
          <h1 className="text-xl font-semibold tracking-tight">
            Development mode
          </h1>
          <p className="text-muted-foreground">
            This server is running with authentication disabled. Requests use
            the configured development user. API keys are not checked, so an
            arbitrary string is not a credential.
          </p>
          <div className="flex justify-end">
            <Button
              type="button"
              size="lg"
              onClick={() => enterDevelopmentMode()}
            >
              Continue in development mode
              <ArrowRight className="size-5" />
            </Button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex min-h-screen w-full items-center justify-center p-4">
      <div className="bg-background flex w-full max-w-xl flex-col rounded-lg border shadow-lg">
        <div className="mt-10 flex flex-col gap-2 border-b p-6">
          <Sprout className="size-7" />
          <h1 className="text-xl font-semibold tracking-tight">
            AgriHub Research Agent
          </h1>
          <p className="text-muted-foreground">
            Enter the platform API key issued for this server. The key is sent
            as <code>X-Api-Key</code> and is kept out of the address bar.
          </p>
        </div>
        <form
          onSubmit={async (event) => {
            event.preventDefault();
            const form = event.currentTarget;
            const formData = new FormData(form);
            const apiKey = String(formData.get("apiKey") ?? "").trim();
            if (!apiKey) {
              setLocalNotice("An API key is required.");
              return;
            }
            setSubmitting(true);
            setLocalNotice(null);
            const result = await probeApi(API_URL, apiKey);
            setSubmitting(false);
            if (result === "unauthorized") {
              setLocalNotice(
                "That API key was rejected. Check that it is active and belongs to an enabled user.",
              );
              return;
            }
            if (result === "unreachable") {
              setLocalNotice(
                "The AgriHub server could not be reached. This is a connection problem, not a rejected key.",
              );
              return;
            }
            if (result === "server") {
              setLocalNotice(
                "The AgriHub server returned an error before accepting the key.",
              );
              return;
            }
            saveApiKey(apiKey, remember);
            form.reset();
          }}
          className="bg-muted/50 flex flex-col gap-6 p-6"
        >
          <div className="flex flex-col gap-2">
            <Label htmlFor="apiKey">
              Platform API key<span className="text-rose-500">*</span>
            </Label>
            <PasswordInput
              id="apiKey"
              name="apiKey"
              autoComplete="off"
              className="bg-background"
              placeholder="Platform API key"
              required
            />
          </div>
          <label className="flex items-start gap-3 text-sm">
            <input
              type="checkbox"
              className="mt-1"
              checked={remember}
              onChange={(event) => setRemember(event.target.checked)}
            />
            <span>
              <span className="font-medium">Remember this key</span>
              <span className="text-muted-foreground block">
                Off by default. The key stays in session storage and is
                discarded when the tab closes. Turning this on stores it in
                localStorage, which any script on this page can read if the site
                is compromised.
              </span>
            </span>
          </label>
          {message ? (
            <p
              role="status"
              className="text-sm text-rose-600"
            >
              {message}
            </p>
          ) : (
            <p className="text-muted-foreground text-sm">
              Waiting for a platform API key. Server: {API_URL}
            </p>
          )}
          <div className="flex justify-end">
            <Button
              type="submit"
              size="lg"
              disabled={submitting}
            >
              {submitting ? "Checking" : "Continue"}
              <ArrowRight className="size-5" />
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}

export const StreamProvider: React.FC<{ children: ReactNode }> = ({
  children,
}) => {
  const { apiKey, devMode, ready, sessionGeneration } = useApiKey();
  if (!ready) {
    return <div className="min-h-screen" />;
  }
  if (!apiKey && !devMode) {
    return <ApiKeyGate />;
  }
  return (
    <StreamSession
      key={sessionGeneration}
      apiKey={apiKey}
      apiUrl={API_URL}
      assistantId={ASSISTANT_ID}
    >
      {children}
    </StreamSession>
  );
};

export const useStreamContext = (): StreamContextType => {
  const context = useContext(StreamContext);
  if (context === undefined) {
    throw new Error("useStreamContext must be used within a StreamProvider");
  }
  return context;
};

export default StreamContext;
