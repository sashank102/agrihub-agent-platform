import { useCallback, useEffect, useRef } from "react";
import { parseSnpText, type ParseOptions, type ParseResult } from "./snp-parse";
import type { ParseResponse } from "./snp-parse.worker";

type Request = {
  text: string;
  options: ParseOptions;
  resolve: (result: ParseResult) => void;
};

export function useSnpParser() {
  const worker = useRef<Worker | null>(null);
  const pending = useRef(new Map<number, Request>());
  const counter = useRef(0);
  const broken = useRef(false);

  useEffect(
    () => () => {
      worker.current?.terminate();
      worker.current = null;
    },
    [],
  );

  return useCallback(
    (text: string, options: ParseOptions): Promise<ParseResult> =>
      new Promise((resolve) => {
        if (typeof Worker === "undefined" || broken.current) {
          resolve(parseSnpText(text, options));
          return;
        }
        if (!worker.current) {
          const instance = new Worker(
            new URL("./snp-parse.worker.ts", import.meta.url),
            { type: "module" },
          );
          instance.addEventListener(
            "message",
            (event: MessageEvent<ParseResponse>) => {
              const request = pending.current.get(event.data.id);
              pending.current.delete(event.data.id);
              request?.resolve(event.data.result);
            },
          );
          instance.addEventListener("error", () => {
            broken.current = true;
            instance.terminate();
            worker.current = null;
            for (const request of pending.current.values()) {
              request.resolve(parseSnpText(request.text, request.options));
            }
            pending.current.clear();
          });
          worker.current = instance;
        }
        counter.current += 1;
        pending.current.set(counter.current, { text, options, resolve });
        worker.current.postMessage({ id: counter.current, text, options });
      }),
    [],
  );
}
