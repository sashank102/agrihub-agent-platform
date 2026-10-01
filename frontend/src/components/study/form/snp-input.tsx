"use client";

import { FileUp, X } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Textarea } from "@/components/ui/textarea";
import {
  COLUMN_ROLES,
  MAX_INPUT_BYTES,
  parseSnpText,
  type ColumnMapping,
  type ColumnRole,
  type ParseOptions,
  type ParseResult,
} from "@/lib/snp-parse";
import { useSnpParser } from "@/lib/use-snp-parser";

const ROLE_LABELS: Record<ColumnRole, string> = {
  id: "SNP id",
  chrom: "Chromosome",
  pos: "Position",
  score: "Score",
  p_value: "p-value",
  method: "Method",
};

export const EXAMPLE_SNPS = "S5_2899164\nS18_9263941\nS18_51620945";

type Source =
  | { kind: "paste"; text: string }
  | { kind: "file"; name: string; text: string };

export function SnpInput({
  options,
  onParsed,
  invalid,
}: {
  options: ParseOptions;
  onParsed: (result: ParseResult | null) => void;
  invalid?: boolean;
}) {
  const parseInWorker = useSnpParser();
  const [source, setSource] = useState<Source>({ kind: "paste", text: "" });
  const [mapping, setMapping] = useState<ColumnMapping | null>(null);
  const [result, setResult] = useState<ParseResult | null>(null);
  const [fileError, setFileError] = useState<string | null>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const textareaId = useId();
  const latest = useRef(0);

  useEffect(() => {
    const ticket = ++latest.current;
    const text = source.text;
    const parseOptions = { ...options, mapping };
    const run = (): Promise<ParseResult | null> =>
      !text.trim()
        ? Promise.resolve(null)
        : source.kind === "file"
          ? parseInWorker(text, parseOptions)
          : Promise.resolve(parseSnpText(text, parseOptions));
    const timer = setTimeout(
      () => {
        void run().then((parsed) => {
          if (ticket === latest.current) {
            setResult(parsed);
            onParsed(parsed);
          }
        });
      },
      source.kind === "file" || !text.trim() ? 0 : 250,
    );
    return () => clearTimeout(timer);
  }, [source, mapping, options, onParsed, parseInWorker]);

  async function loadFile(file: File) {
    setFileError(null);
    if (file.size > MAX_INPUT_BYTES) {
      setFileError(
        `${file.name} is ${(file.size / 1024 / 1024).toFixed(1)} MB; the limit is 5 MB.`,
      );
      return;
    }
    setMapping(null);
    setSource({ kind: "file", name: file.name, text: await file.text() });
  }

  const header = result?.header ?? null;
  const showMapping =
    result?.format === "table" &&
    header !== null &&
    (result.needsMapping || mapping !== null);

  return (
    <div className="flex flex-col gap-3">
      <Tabs
        value={source.kind}
        onValueChange={(value) => {
          setMapping(null);
          setSource(
            value === "paste"
              ? { kind: "paste", text: "" }
              : { kind: "file", name: "", text: "" },
          );
        }}
      >
        <TabsList>
          <TabsTrigger value="paste">Paste</TabsTrigger>
          <TabsTrigger value="file">Upload file</TabsTrigger>
        </TabsList>
        <TabsContent
          value="paste"
          className="flex flex-col gap-2"
        >
          <Label
            htmlFor={textareaId}
            className="sr-only"
          >
            SNP list
          </Label>
          <Textarea
            id={textareaId}
            aria-invalid={invalid || undefined}
            value={source.kind === "paste" ? source.text : ""}
            onChange={(event) =>
              setSource({ kind: "paste", text: event.target.value })
            }
            rows={7}
            spellCheck={false}
            className="font-mono text-sm"
            placeholder={
              "One SNP per line: S18_9263941, Chr18:9263941, 18 9263941, 18,9263941,\nss/rs/BARC ids, or paste a CSV/TSV with a header row."
            }
          />
          <div className="flex items-center gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setSource({ kind: "paste", text: EXAMPLE_SNPS })}
            >
              Use the poster SNPs
            </Button>
            {source.kind === "paste" && source.text && (
              <Button
                type="button"
                variant="ghost"
                size="sm"
                onClick={() => setSource({ kind: "paste", text: "" })}
              >
                <X />
                Clear
              </Button>
            )}
          </div>
        </TabsContent>
        <TabsContent
          value="file"
          className="flex flex-col gap-2"
        >
          <div className="flex flex-wrap items-center gap-3 rounded-md border border-dashed p-4">
            <input
              ref={fileInput}
              type="file"
              accept=".csv,.tsv,.txt,.tab,.vcf,text/csv,text/plain"
              className="sr-only"
              aria-label="SNP file"
              data-testid="snp-file"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (file) {
                  void loadFile(file);
                }
                event.target.value = "";
              }}
            />
            <Button
              type="button"
              variant="outline"
              onClick={() => fileInput.current?.click()}
            >
              <FileUp />
              Choose a CSV, TSV or VCF
            </Button>
            <span className="text-muted-foreground text-sm">
              {source.kind === "file" && source.name
                ? source.name
                : "Up to 5 MB. Parsed in the browser."}
            </span>
          </div>
          {fileError && (
            <p
              role="alert"
              className="text-sm text-rose-600"
            >
              {fileError}
            </p>
          )}
        </TabsContent>
      </Tabs>
      {result && (
        <p className="text-muted-foreground text-xs">
          Read as{" "}
          {result.format === "vcf"
            ? "VCF"
            : result.format === "table"
              ? "a table with a header row"
              : "a SNP list"}
          {result.preset === "sister-team" &&
            " using the sister-team preset (rs, chrom, pos, maf, score, rank, method)"}
          .
        </p>
      )}
      {result?.error && (
        <p
          role="alert"
          className="text-sm text-rose-600"
        >
          {result.error}
        </p>
      )}
      {showMapping && header && (
        <ColumnMapper
          header={header}
          mapping={mapping ?? result?.mapping ?? {}}
          needsMapping={result?.needsMapping ?? false}
          onChange={setMapping}
        />
      )}
    </div>
  );
}

function ColumnMapper({
  header,
  mapping,
  needsMapping,
  onChange,
}: {
  header: string[];
  mapping: ColumnMapping;
  needsMapping: boolean;
  onChange: (mapping: ColumnMapping) => void;
}) {
  const baseId = useId();
  return (
    <fieldset className="bg-muted/40 flex flex-col gap-3 rounded-md border p-3">
      <legend className="px-1 text-sm font-medium">Map columns</legend>
      {needsMapping && (
        <p className="text-muted-foreground text-xs">
          The header was not recognized. Pick the SNP id column, or the
          chromosome and position columns.
        </p>
      )}
      <div className="grid grid-cols-2 gap-3 sm:grid-cols-3">
        {COLUMN_ROLES.map((role) => (
          <div
            key={role}
            className="flex flex-col gap-1"
          >
            <Label
              htmlFor={`${baseId}-${role}`}
              className="text-xs"
            >
              {ROLE_LABELS[role]}
            </Label>
            <select
              id={`${baseId}-${role}`}
              className="border-input bg-background h-8 rounded-md border px-2 text-sm"
              value={mapping[role] ?? ""}
              onChange={(event) => {
                const next = { ...mapping };
                if (event.target.value === "") {
                  delete next[role];
                } else {
                  next[role] = Number(event.target.value);
                }
                onChange(next);
              }}
            >
              <option value="">Not used</option>
              {header.map((name, index) =>
                name ? (
                  <option
                    key={index}
                    value={index}
                  >
                    {name}
                  </option>
                ) : null,
              )}
            </select>
          </div>
        ))}
      </div>
    </fieldset>
  );
}
