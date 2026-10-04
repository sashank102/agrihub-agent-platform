"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { MarkdownText } from "@/components/thread/markdown-text";
import {
  HoverCard,
  HoverCardContent,
  HoverCardTrigger,
} from "@/components/ui/hover-card";
import type { ReportCitation, ReportSource } from "@/lib/study-api";
import { cn } from "@/lib/utils";
import { CITATION_HREF, SOURCE_PREFIX, linkCitations } from "./exports";

export type CitationHandlers = {
  citations: Map<string, ReportCitation>;
  sources: Map<string, ReportSource>;
  onOpenEvidence: (alias: string) => void;
  onOpenSource: (sourceId: string) => void;
};

const CitationContext = createContext<CitationHandlers | null>(null);

export function CitationProvider({
  value,
  children,
}: {
  value: CitationHandlers;
  children: ReactNode;
}) {
  return (
    <CitationContext.Provider value={value}>
      {children}
    </CitationContext.Provider>
  );
}

function useCitations(): CitationHandlers {
  const value = useContext(CitationContext);
  if (!value) {
    throw new Error("Citations need a CitationProvider.");
  }
  return value;
}

export function Citation({ alias }: { alias: string }) {
  const { citations, onOpenEvidence } = useCitations();
  const citation = citations.get(alias);
  return (
    <HoverCard>
      <HoverCardTrigger asChild>
        <button
          type="button"
          className="text-primary font-mono text-[0.85em] underline-offset-2 hover:underline"
          onClick={() => onOpenEvidence(alias)}
          data-citation={alias}
        >
          [{alias}]
        </button>
      </HoverCardTrigger>
      <HoverCardContent className="w-80 space-y-1 text-left text-xs">
        <p className="font-medium">{citation?.source_db ?? "Unknown source"}</p>
        <p className="text-muted-foreground">
          {citation
            ? `${citation.category} · ${citation.subtype} · ${citation.gene_id}`
            : "This citation does not resolve."}
        </p>
        <p>{citation?.quote || "No verbatim quote was stored."}</p>
        <p className="text-muted-foreground">
          Verifier: {citation?.verifier_status ?? "unchecked"}
        </p>
      </HoverCardContent>
    </HoverCard>
  );
}

function SourceCitation({ sourceId }: { sourceId: string }) {
  const { sources, onOpenSource } = useCitations();
  const source = sources.get(sourceId);
  return (
    <button
      type="button"
      className="text-primary text-[0.85em] underline-offset-2 hover:underline"
      title={
        source
          ? `${source.name} ${source.version}`.trim()
          : "This source does not resolve."
      }
      onClick={() => onOpenSource(sourceId)}
      data-source-citation={sourceId}
    >
      [{source?.name ?? sourceId}]
    </button>
  );
}

/** Render Markdown whose `[E12]` and `[source:x]` markers become interactive citations. */
export function CitationText({
  markdown,
  className,
}: {
  markdown: string;
  className?: string;
}) {
  const linked = useMemo(() => linkCitations(markdown), [markdown]);
  const components = useMemo(
    () => ({
      a: ({ href, children }: { href?: string; children?: ReactNode }) => {
        if (href?.startsWith(CITATION_HREF)) {
          const token = href.slice(CITATION_HREF.length);
          return token.startsWith(SOURCE_PREFIX) ? (
            <SourceCitation sourceId={token.slice(SOURCE_PREFIX.length)} />
          ) : (
            <Citation alias={token} />
          );
        }
        return (
          <a
            href={href}
            target="_blank"
            rel="noreferrer"
            className="text-primary font-medium underline underline-offset-4"
          >
            {children}
          </a>
        );
      },
    }),
    [],
  );
  return (
    <div className={cn("text-sm", className)}>
      <MarkdownText components={components}>{linked}</MarkdownText>
    </div>
  );
}
