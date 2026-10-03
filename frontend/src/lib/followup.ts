import type { StudyReport } from "./study-api";

/** Return the messages after the study's report: the follow-up questions and answers. */
export function followupMessages<T extends { type: string; content: unknown }>(
  messages: T[],
  report: Pick<StudyReport, "markdown"> | null | undefined,
): T[] {
  const markdown = report?.markdown;
  if (!markdown) {
    return messages;
  }
  let start = 0;
  messages.forEach((message, index) => {
    if (message.type === "ai" && message.content === markdown) {
      start = index + 1;
    }
  });
  return messages.slice(start);
}
