"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet";
import type { CandidateRow } from "@/lib/run-events";
import type { StudyReport } from "@/lib/study-api";
import { ArtifactProvider } from "@/components/thread/artifact";
import { AssistantMessage } from "@/components/thread/messages/ai";
import { HumanMessage } from "@/components/thread/messages/human";
import { useStreamContext } from "@/providers/Stream";

function suggestions(report: StudyReport | null | undefined): string[] {
  const genes = (report?.candidates_full ??
    report?.candidates ??
    []) as CandidateRow[];
  const first = genes[0];
  const second =
    genes.find((item) => item.locus_id !== first?.locus_id) ?? genes[1];
  const questions = [];
  if (first) {
    questions.push(`Why is ${first.gene_id} a candidate?`);
  }
  if (first && second) {
    questions.push(`Why is ${first.gene_id} ranked above ${second.gene_id}?`);
  }
  const conflict = genes.find(
    (item) => (item.conflicting_findings?.length ?? 0) > 0,
  );
  if (conflict) {
    questions.push(`What conflicts exist for ${conflict.locus_id}?`);
  }
  return questions;
}

export function QAPanel({
  report,
  onOpenEvidence,
}: {
  report: StudyReport | null | undefined;
  onOpenEvidence: (alias: string) => void;
}) {
  const stream = useStreamContext();
  const [question, setQuestion] = useState("");
  const ask = (text: string) => {
    const trimmed = text.trim();
    if (!trimmed || stream.isLoading) {
      return;
    }
    setQuestion("");
    void stream.submit(
      { followup: trimmed },
      {
        streamMode: ["values", "messages", "custom"],
        streamSubgraphs: false,
      },
    );
  };
  const messages = stream.messages ?? [];
  return (
    <Sheet>
      <SheetTrigger asChild>
        <Button
          type="button"
          variant="outline"
          data-testid="open-qa"
        >
          Ask about this study
        </Button>
      </SheetTrigger>
      <SheetContent
        className="flex w-full flex-col gap-3 sm:max-w-md"
        data-testid="qa-panel"
      >
        <SheetHeader>
          <SheetTitle>Follow-up questions</SheetTitle>
          <SheetDescription>
            Answers cite this study&apos;s evidence and do not change it.
          </SheetDescription>
        </SheetHeader>
        <div className="flex flex-wrap gap-2">
          {suggestions(report).map((item) => (
            <Button
              key={item}
              type="button"
              variant="secondary"
              size="sm"
              onClick={() => ask(item)}
            >
              {item}
            </Button>
          ))}
        </div>
        <ArtifactProvider>
          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto">
            {messages.map((message, index) =>
              message.type === "human" ? (
                <HumanMessage
                  key={message.id ?? index}
                  message={message}
                  isLoading={stream.isLoading}
                />
              ) : message.type === "ai" ? (
                <AssistantMessage
                  key={message.id ?? index}
                  message={message}
                  isLoading={stream.isLoading}
                  handleRegenerate={() => undefined}
                />
              ) : null,
            )}
          </div>
        </ArtifactProvider>
        <EvidenceLinks
          text={messages
            .filter((message) => message.type === "ai")
            .map((message) =>
              typeof message.content === "string" ? message.content : "",
            )
            .join("\n")}
          onOpen={onOpenEvidence}
        />
        <form
          className="flex gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            ask(question);
          }}
        >
          <Input
            value={question}
            onChange={(event) => setQuestion(event.target.value)}
            aria-label="Follow-up question"
            placeholder="Ask about a gene or locus"
          />
          <Button
            type="submit"
            disabled={stream.isLoading}
          >
            Ask
          </Button>
        </form>
      </SheetContent>
    </Sheet>
  );
}

function EvidenceLinks({
  text,
  onOpen,
}: {
  text: string;
  onOpen: (alias: string) => void;
}) {
  const aliases = [...text.matchAll(/\[(E\d+)\]/g)].map((match) => match[1]);
  const unique = [...new Set(aliases)];
  if (unique.length === 0) {
    return null;
  }
  return (
    <p className="text-xs">
      Evidence:{" "}
      {unique.map((alias) => (
        <button
          key={alias}
          type="button"
          className="text-primary mr-2 font-mono underline"
          onClick={() => onOpen(alias ?? "")}
        >
          {alias}
        </button>
      ))}
    </p>
  );
}
