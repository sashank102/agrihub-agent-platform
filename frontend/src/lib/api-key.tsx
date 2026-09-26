"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

const SESSION_KEY = "agrihub.platformApiKey";
const LOCAL_KEY = "agrihub.platformApiKey";
const REMEMBER_FLAG = "agrihub.rememberApiKey";

export type AuthNotice = "rejected" | "unreachable" | "server" | null;

function readStoredKey(): string {
  if (typeof window === "undefined") {
    return "";
  }
  try {
    const sessionKey = window.sessionStorage.getItem(SESSION_KEY);
    if (sessionKey) {
      return sessionKey;
    }
    if (window.localStorage.getItem(REMEMBER_FLAG) === "1") {
      return window.localStorage.getItem(LOCAL_KEY) ?? "";
    }
  } catch {
    return "";
  }
  return "";
}

function writeStoredKey(key: string, remember: boolean): void {
  window.sessionStorage.setItem(SESSION_KEY, key);
  if (remember) {
    window.localStorage.setItem(REMEMBER_FLAG, "1");
    window.localStorage.setItem(LOCAL_KEY, key);
    return;
  }
  window.localStorage.removeItem(REMEMBER_FLAG);
  window.localStorage.removeItem(LOCAL_KEY);
}

function eraseStoredKey(): void {
  try {
    window.sessionStorage.removeItem(SESSION_KEY);
    window.localStorage.removeItem(LOCAL_KEY);
    window.localStorage.removeItem(REMEMBER_FLAG);
  } catch {
    // Storage can be unavailable in private contexts. The in-memory key is cleared too.
  }
}

export function getApiKey(): string | null {
  const key = readStoredKey();
  return key || null;
}

export function isUnauthorizedStatus(error: unknown): boolean {
  if (typeof error !== "object" || error === null) {
    return false;
  }
  const status = "status" in error ? error.status : undefined;
  if (status === 401) {
    return true;
  }
  const response = "response" in error ? error.response : undefined;
  if (
    typeof response === "object" &&
    response !== null &&
    "status" in response &&
    response.status === 401
  ) {
    return true;
  }
  return false;
}

type ApiKeyContextValue = {
  apiKey: string;
  devMode: boolean;
  ready: boolean;
  notice: AuthNotice;
  sessionGeneration: number;
  connection: "unknown" | "ok" | "unreachable" | "server";
  setConnection: (value: "unknown" | "ok" | "unreachable" | "server") => void;
  saveApiKey: (key: string, remember: boolean) => void;
  clearApiKey: (notice?: AuthNotice) => void;
  enterDevelopmentMode: () => void;
};

let tenantReset: () => void = () => undefined;

export function registerTenantReset(reset: () => void): void {
  tenantReset = reset;
}

const ApiKeyContext = createContext<ApiKeyContextValue | undefined>(undefined);

export function ApiKeyProvider({ children }: { children: ReactNode }) {
  const [apiKey, setApiKey] = useState("");
  const [devMode, setDevMode] = useState(false);
  const [ready, setReady] = useState(false);
  const [notice, setNotice] = useState<AuthNotice>(null);
  const [sessionGeneration, setSessionGeneration] = useState(0);
  const [connection, setConnection] = useState<
    "unknown" | "ok" | "unreachable" | "server"
  >("unknown");

  useEffect(() => {
    setApiKey(readStoredKey());
    setReady(true);
  }, []);

  const forgetTenant = useCallback(() => {
    tenantReset();
    setSessionGeneration((current) => current + 1);
  }, []);

  const saveApiKey = useCallback(
    (key: string, remember: boolean) => {
      forgetTenant();
      eraseStoredKey();
      setDevMode(false);
      writeStoredKey(key, remember);
      setApiKey(key);
      setNotice(null);
      setConnection("unknown");
    },
    [forgetTenant],
  );

  const clearApiKey = useCallback(
    (nextNotice: AuthNotice = null) => {
      forgetTenant();
      eraseStoredKey();
      setDevMode(false);
      setApiKey("");
      setNotice(nextNotice);
      setConnection("unknown");
    },
    [forgetTenant],
  );

  const enterDevelopmentMode = useCallback(() => {
    forgetTenant();
    eraseStoredKey();
    setApiKey("");
    setDevMode(true);
    setNotice(null);
    setConnection("ok");
  }, [forgetTenant]);

  const value = useMemo(
    () => ({
      apiKey,
      devMode,
      ready,
      notice,
      sessionGeneration,
      connection,
      setConnection,
      saveApiKey,
      clearApiKey,
      enterDevelopmentMode,
    }),
    [
      apiKey,
      devMode,
      ready,
      notice,
      sessionGeneration,
      connection,
      saveApiKey,
      clearApiKey,
      enterDevelopmentMode,
    ],
  );

  return (
    <ApiKeyContext.Provider value={value}>{children}</ApiKeyContext.Provider>
  );
}

export function useApiKey(): ApiKeyContextValue {
  const context = useContext(ApiKeyContext);
  if (context === undefined) {
    throw new Error("useApiKey must be used within an ApiKeyProvider");
  }
  return context;
}
