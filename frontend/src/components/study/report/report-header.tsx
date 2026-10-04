"use client";

import {
  ChevronDown,
  Download,
  FileJson,
  FileSpreadsheet,
  FileText,
  MessageSquareText,
  Printer,
} from "lucide-react";
import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/popover";
import type { StudyReport } from "@/lib/study-api";
import { RunStatusBadge } from "../run-status-badge";
import type { StudyChips } from "../run/run-header";
import {
  candidatesCsv,
  download,
  evidenceCsv,
  reportJson,
  reportMarkdown,
} from "./exports";
import { QAPanel } from "./qa-panel";

function DownloadMenu({ report }: { report: StudyReport | null }) {
  const [open, setOpen] = useState(false);
  const slug = (report?.trait || "study").replaceAll(/\s+/g, "-");
  const items = [
    {
      label: "JSON",
      detail: "The full report with provenance",
      icon: FileJson,
      run: (value: StudyReport) =>
        download(`${slug}.json`, reportJson(value), "application/json"),
    },
    {
      label: "Candidates CSV",
      detail: "Every scored gene with points A–G",
      icon: FileSpreadsheet,
      run: (value: StudyReport) =>
        download(`${slug}-candidates.csv`, candidatesCsv(value), "text/csv"),
    },
    {
      label: "Evidence CSV",
      detail: "Every cited evidence item and quote",
      icon: FileSpreadsheet,
      run: (value: StudyReport) =>
        download(`${slug}-evidence.csv`, evidenceCsv(value), "text/csv"),
    },
    {
      label: "Markdown",
      detail: "The summary and the per-locus tables",
      icon: FileText,
      run: (value: StudyReport) =>
        download(`${slug}.md`, reportMarkdown(value), "text/markdown"),
    },
  ];
  return (
    <Popover
      open={open}
      onOpenChange={setOpen}
    >
      <PopoverTrigger asChild>
        <Button
          type="button"
          variant="outline"
          data-testid="open-downloads"
        >
          <Download />
          Download
          <ChevronDown className="opacity-60" />
        </Button>
      </PopoverTrigger>
      <PopoverContent
        align="end"
        className="w-72 p-1"
      >
        <ul
          aria-label="Download formats"
          className="flex flex-col"
        >
          {items.map((item) => (
            <li key={item.label}>
              <Button
                type="button"
                variant="ghost"
                className="h-auto w-full justify-start gap-3 px-2 py-2 text-left"
                disabled={!report}
                onClick={() => {
                  if (report) {
                    item.run(report);
                  }
                  setOpen(false);
                }}
              >
                <item.icon className="text-muted-foreground" />
                <span className="flex flex-col">
                  <span>{item.label}</span>
                  <span className="text-muted-foreground text-xs font-normal">
                    {item.detail}
                  </span>
                </span>
              </Button>
            </li>
          ))}
          <li className="mt-1 border-t pt-1">
            <Button
              type="button"
              variant="ghost"
              className="h-auto w-full justify-start gap-3 px-2 py-2 text-left"
              onClick={() => {
                setOpen(false);
                window.setTimeout(() => window.print(), 0);
              }}
            >
              <Printer className="text-muted-foreground" />
              <span className="flex flex-col">
                <span>Print PDF</span>
                <span className="text-muted-foreground text-xs font-normal">
                  The summary and candidates, print-formatted
                </span>
              </span>
            </Button>
          </li>
        </ul>
      </PopoverContent>
    </Popover>
  );
}

/** The finished study's title bar: what was studied, its status, and the Ask and Download actions. */
export function ReportHeader({
  report,
  chips,
  status,
}: {
  report: StudyReport | null;
  chips: StudyChips;
  status: string;
}) {
  const species = report?.species || chips.species;
  const assembly = report?.assembly || chips.assembly;
  const trait = report?.trait || chips.trait;
  return (
    <header
      className="flex flex-wrap items-center gap-x-4 gap-y-2"
      data-testid="report-header"
    >
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-2">
        <h1 className="truncate text-xl font-semibold tracking-tight first-letter:uppercase">
          {trait ?? "Study"}
        </h1>
        <ul
          aria-label="Study"
          className="flex flex-wrap items-center gap-1.5"
        >
          {species && (
            <li>
              <Badge
                variant="secondary"
                className="capitalize"
              >
                {species}
              </Badge>
            </li>
          )}
          {assembly && (
            <li>
              <Badge variant="secondary">{assembly}</Badge>
            </li>
          )}
          <li>
            <RunStatusBadge status={status} />
          </li>
        </ul>
      </div>
      <div className="flex items-center gap-2 print:hidden">
        <QAPanel
          report={report}
          onOpenEvidence={() => undefined}
          trigger={
            <Button
              type="button"
              data-testid="open-qa"
            >
              <MessageSquareText />
              Ask about this study
            </Button>
          }
        />
        <DownloadMenu report={report} />
      </div>
    </header>
  );
}
