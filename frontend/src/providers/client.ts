import { Client } from "@langchain/langgraph-sdk";

export function createClient(apiUrl: string, apiKey: string | undefined) {
  return new Client({
    apiUrl,
    apiKey,
    defaultHeaders: apiKey ? { "X-Api-Key": apiKey } : undefined,
  });
}
