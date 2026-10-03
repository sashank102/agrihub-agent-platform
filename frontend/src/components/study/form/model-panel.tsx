"use client";

import { AlertTriangle, Loader2 } from "lucide-react";
import { useEffect, useId, useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { isUnauthorizedStatus, useApiKey } from "@/lib/api-key";
import {
  useStudyApi,
  type ModelCandidate,
  type ModelCatalog,
} from "@/lib/study-api";
import { cn } from "@/lib/utils";
import { modelQueryKey } from "./study-schema";

const SCORE_TYPES: Record<string, string> = {
  p_value: "p-value",
  gnnexplainer: "GNNExplainer importance",
  integrated_gradients: "Integrated Gradients",
  weight: "model weight",
};

function choiceKey(candidate: ModelCandidate, dataset: string | null) {
  return dataset ? `${candidate.model_id}:${dataset}` : candidate.model_id;
}

/** Lists the registered models for a trait study, or says that none applies. */
export function ModelPanel({
  species,
  trait,
  assembly,
  value,
  onChange,
  onCatalog,
}: {
  species: string;
  trait: string;
  assembly: string;
  value: string[];
  onChange: (preferences: string[]) => void;
  onCatalog: (key: string, catalog: ModelCatalog) => void;
}) {
  const api = useStudyApi();
  const { clearApiKey } = useApiKey();
  const ids = useId();
  const [loaded, setLoaded] = useState<{
    key: string;
    catalog: ModelCatalog;
  } | null>(null);
  const query = trait.trim();
  const key = modelQueryKey(species, trait, assembly);

  useEffect(() => {
    if (!query) {
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      api
        .models({ species, trait: query, assembly }, controller.signal)
        .then((result) => {
          setLoaded({ key, catalog: result });
          onCatalog(key, result);
        })
        .catch((error: unknown) => {
          if (isUnauthorizedStatus(error)) {
            clearApiKey("rejected");
          }
        });
    }, 400);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, assembly, clearApiKey, key, onCatalog, query, species]);

  const catalog = loaded?.key === key ? loaded.catalog : null;
  if (!query) {
    return (
      <p className="text-muted-foreground text-sm">
        Enter a trait to see which models or precomputed results can propose
        SNPs for it.
      </p>
    );
  }
  if (!catalog) {
    return (
      <p className="text-muted-foreground flex items-center gap-2 text-sm">
        <Loader2 className="size-4 animate-spin" />
        Looking up models for {query}
      </p>
    );
  }
  const applicable = catalog.models.filter((item) => item.applicability.ok);
  const other = catalog.models.filter((item) => !item.applicability.ok);
  const options = applicable.flatMap((candidate) =>
    candidate.applicability.datasets.length
      ? candidate.applicability.datasets.map((dataset) => ({
          candidate,
          dataset,
        }))
      : [{ candidate, dataset: null as string | null }],
  );
  const selected = value[0] ?? "";
  return (
    <div
      className="flex flex-col gap-3"
      data-testid="model-panel"
    >
      {applicable.length === 0 ? (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-md border border-amber-300 bg-amber-50/60 p-3 text-sm text-amber-800 dark:bg-amber-950/20"
          data-testid="no-model"
        >
          <AlertTriangle className="mt-0.5 size-4 shrink-0" />
          <div className="flex flex-col gap-1">
            <p className="font-medium">No applicable model registered</p>
            <p>
              {catalog.detail ||
                `No model or precomputed result covers ${query} in ${species}.`}{" "}
              Switch to a SNP list, or register a model for this trait.
            </p>
          </div>
        </div>
      ) : (
        <RadioGroup
          value={selected || "auto"}
          onValueChange={(next) => onChange(next === "auto" ? [] : [next])}
          aria-label="Model or precomputed dataset"
          className="grid gap-1.5"
        >
          <Label
            htmlFor={`${ids}-auto`}
            className={cn(
              "flex cursor-pointer items-start gap-3 rounded-md border p-2.5 font-normal",
              selected === "" && "border-foreground/40 bg-muted/50",
            )}
          >
            <RadioGroupItem
              id={`${ids}-auto`}
              value="auto"
              className="mt-0.5"
            />
            <span className="flex flex-col gap-0.5">
              <span className="font-medium">Let the model agent choose</span>
              <span className="text-muted-foreground text-xs">
                It prefers results computed for this trait over published
                catalog hits and states why.
              </span>
            </span>
          </Label>
          {options.map(({ candidate, dataset }) => {
            const key = choiceKey(candidate, dataset);
            return (
              <Label
                key={key}
                htmlFor={`${ids}-${key}`}
                className={cn(
                  "flex cursor-pointer items-start gap-3 rounded-md border p-2.5 font-normal",
                  selected === key && "border-foreground/40 bg-muted/50",
                )}
              >
                <RadioGroupItem
                  id={`${ids}-${key}`}
                  value={key}
                  className="mt-0.5"
                />
                <span className="flex min-w-0 flex-col gap-1">
                  <span className="flex flex-wrap items-center gap-2">
                    <span className="font-medium">{candidate.name}</span>
                    {dataset && <Badge variant="info">{dataset}</Badge>}
                    <Badge variant="outline">
                      {SCORE_TYPES[candidate.score_type] ??
                        candidate.score_type}
                    </Badge>
                  </span>
                  <span className="text-muted-foreground text-xs">
                    {candidate.label}
                    {candidate.assembly !== assembly
                      ? `; positions on ${candidate.assembly} are lifted to the study assembly`
                      : ""}
                  </span>
                </span>
              </Label>
            );
          })}
        </RadioGroup>
      )}
      {other.length > 0 && (
        <details className="text-muted-foreground text-xs">
          <summary className="cursor-pointer">
            {other.length} registered model{other.length === 1 ? "" : "s"} not
            applicable
          </summary>
          <ul className="mt-1 flex flex-col gap-1">
            {other.map((item) => (
              <li key={item.model_id}>
                <span className="text-foreground">{item.name}</span>:{" "}
                {item.applicability.reasons.join("; ")}
              </li>
            ))}
          </ul>
        </details>
      )}
    </div>
  );
}
