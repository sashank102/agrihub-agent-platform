"use client";

import { AlertTriangle } from "lucide-react";
import { useId, useMemo } from "react";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import {
  OUT_OF_BOUNDS_WARN_SHARE,
  assemblyFit,
  type ParsedSnp,
} from "@/lib/snp-parse";
import type { SpeciesInfo } from "@/lib/study-api";
import { cn } from "@/lib/utils";

function percent(share: number): string {
  return `${(share * 100).toFixed(share < 0.1 && share > 0 ? 1 : 0)}%`;
}

export function SisterTeamAssembly({
  species,
  rows,
  value,
  confirmed,
  onChoose,
}: {
  species: SpeciesInfo;
  rows: ParsedSnp[];
  value: string;
  confirmed: boolean;
  onChoose: (assembly: string) => void;
}) {
  const ids = useId();
  const fits = useMemo(
    () =>
      assemblyFit(rows, species.assemblies, species.chromosome_prefixes ?? []),
    [rows, species],
  );
  const chosen = fits.find((fit) => fit.assembly === value);
  const best = fits.reduce<(typeof fits)[number] | null>(
    (current, fit) =>
      fit.checked > 0 && (current === null || fit.share < current.share)
        ? fit
        : current,
    null,
  );
  return (
    <fieldset
      className="flex flex-col gap-3 rounded-md border border-amber-300 bg-amber-50/60 p-3 dark:bg-amber-950/20"
      data-testid="sister-team-assembly"
    >
      <legend className="px-1 text-sm font-medium">
        Which assembly is the pos column on?
      </legend>
      <p className="text-muted-foreground text-xs">
        Sister-team files do not say which reference their positions use. Pick
        it before starting: positions on a Lee assembly are lifted to{" "}
        {species.canonical_assembly} through one-to-one pangene gene anchors,
        and each SNP keeps its original position. Confirm the assembly with the
        team that produced the file.
      </p>
      <RadioGroup
        value={confirmed ? value : ""}
        onValueChange={onChoose}
        aria-label="Assembly of the sister-team positions"
        className="grid gap-1.5"
      >
        {fits.map((fit) => {
          const assembly = species.assemblies.find(
            (item) => item.id === fit.assembly,
          );
          const over = fit.share > OUT_OF_BOUNDS_WARN_SHARE;
          return (
            <Label
              key={fit.assembly}
              htmlFor={`${ids}-${fit.assembly}`}
              className={cn(
                "bg-background flex cursor-pointer items-center gap-3 rounded-md border px-3 py-2 font-normal",
                confirmed && value === fit.assembly && "border-foreground/40",
              )}
            >
              <RadioGroupItem
                id={`${ids}-${fit.assembly}`}
                value={fit.assembly}
              />
              <span className="font-medium">{fit.assembly}</span>
              {assembly?.lift_to && (
                <Badge variant="info">lifted to {assembly.lift_to}</Badge>
              )}
              {best?.assembly === fit.assembly && fit.beyond === 0 && (
                <Badge variant="success">all positions fit</Badge>
              )}
              <span
                className={cn(
                  "ml-auto text-xs tabular-nums",
                  over ? "text-amber-700" : "text-muted-foreground",
                )}
              >
                {fit.beyond} of {fit.checked} past chromosome ends (
                {percent(fit.share)})
              </span>
            </Label>
          );
        })}
      </RadioGroup>
      {!confirmed && (
        <p
          role="alert"
          className="text-sm text-rose-600"
        >
          Choose the assembly the positions are on to start the study.
        </p>
      )}
      {confirmed && chosen && chosen.share > OUT_OF_BOUNDS_WARN_SHARE && (
        <p
          role="status"
          className="flex items-start gap-2 text-sm text-amber-700"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          {chosen.beyond} of {chosen.checked} rows ({percent(chosen.share)})
          fall past chromosome ends on {chosen.assembly}; the positions are
          probably on another assembly.
        </p>
      )}
    </fieldset>
  );
}
