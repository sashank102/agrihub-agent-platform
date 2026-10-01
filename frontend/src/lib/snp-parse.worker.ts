import { parseSnpText, type ParseOptions, type ParseResult } from "./snp-parse";

export type ParseRequest = { id: number; text: string; options: ParseOptions };
export type ParseResponse = { id: number; result: ParseResult };

const scope = globalThis as unknown as {
  addEventListener: (
    type: "message",
    listener: (event: MessageEvent<ParseRequest>) => void,
  ) => void;
  postMessage: (message: ParseResponse) => void;
};

scope.addEventListener("message", (event) => {
  const { id, text, options } = event.data;
  scope.postMessage({ id, result: parseSnpText(text, options) });
});
