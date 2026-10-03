import { describe, expect, it } from "vitest";
import { followupMessages } from "./followup";

describe("follow-up messages", () => {
  it("drops the study summary and the report so only questions and answers remain", () => {
    const messages = [
      { type: "human", content: "Study: plant height" },
      { type: "ai", content: "# report [E1]" },
      { type: "human", content: "why is Glyma.18G092200 a candidate?" },
      { type: "ai", content: "Because [E2]." },
    ];
    expect(followupMessages(messages, { markdown: "# report [E1]" })).toEqual(
      messages.slice(2),
    );
    expect(followupMessages(messages, { markdown: "" })).toEqual(messages);
    expect(
      followupMessages(messages.slice(0, 2), { markdown: "# report [E1]" }),
    ).toEqual([]);
  });
});
