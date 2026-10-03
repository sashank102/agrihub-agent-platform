"use client";

import { zodResolver } from "@hookform/resolvers/zod";
import { AlertTriangle, Info, Loader2, Play } from "lucide-react";
import {
  useCallback,
  useEffect,
  useId,
  useMemo,
  useRef,
  useState,
} from "react";
import { Controller, useForm, useWatch } from "react-hook-form";
import { toast } from "sonner";
import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Checkbox } from "@/components/ui/checkbox";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { RadioGroup, RadioGroupItem } from "@/components/ui/radio-group";
import { Slider } from "@/components/ui/slider";
import { isUnauthorizedStatus, useApiKey } from "@/lib/api-key";
import { SPECIALISTS } from "@/lib/run-events";
import { useRunStore } from "@/lib/run-store";
import {
  mergeValidation,
  submittableSnps,
  type ParseOptions,
  type ParseResult,
} from "@/lib/snp-parse";
import {
  STUDY_ASSISTANT_ID,
  useStudyApi,
  type SpeciesInfo,
  type StudyMetadata,
  type StudyValidation,
} from "@/lib/study-api";
import { cn } from "@/lib/utils";
import { useStreamContext } from "@/providers/Stream";
import { useStudySession } from "@/providers/StudyStream";
import { SnpInput } from "./snp-input";
import { SisterTeamAssembly } from "./sister-team-assembly";
import { ServerPreview, SnpPreview } from "./snp-preview";
import {
  SPECIALIST_LABELS,
  TRAIT_SUGGESTIONS,
  defaultWindowKb,
  ldAvailable,
  ldWarningKb,
  studyFormSchema,
  studySummary,
  toStudyRequest,
  type StudyFormValues,
} from "./study-schema";
import { TraitField } from "./trait-field";

const DEFAULTS: StudyFormValues = {
  mode: "snps",
  species: "soybean",
  assembly: "Wm82.a2.v1",
  trait_text: "",
  window_kb: 250,
  window_mode: "fixed",
  ld_r2: 0.2,
  top_k_per_locus: 5,
  specialists_enabled: [...SPECIALISTS],
  assembly_confirmed: true,
  snps: [],
};

function parsedKey(result: ParseResult): string {
  const rows = result.rows;
  return `${rows.length}:${rows[0]?.raw ?? ""}:${rows.at(-1)?.raw ?? ""}`;
}

function FieldError({ id, message }: { id: string; message?: string }) {
  if (!message) {
    return null;
  }
  return (
    <p
      id={id}
      role="alert"
      className="text-sm text-rose-600"
    >
      {message}
    </p>
  );
}

export function StudyForm() {
  const api = useStudyApi();
  const stream = useStreamContext();
  const { clearApiKey } = useApiKey();
  const { beginNewStudy } = useStudySession();
  const ids = useId();
  const [registry, setRegistry] = useState<SpeciesInfo[] | null>(null);
  const [registryError, setRegistryError] = useState<string | null>(null);
  const [parsed, setParsed] = useState<ParseResult | null>(null);
  const [validation, setValidation] = useState<StudyValidation | null>(null);
  const [validating, setValidating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const presetSeen = useRef<string | null>(null);

  const schema = useMemo(() => studyFormSchema(registry ?? []), [registry]);
  const form = useForm<StudyFormValues>({
    resolver: zodResolver(schema),
    defaultValues: DEFAULTS,
    mode: "onTouched",
  });
  const { control, formState, setValue, getValues, reset } = form;
  const values = useWatch({ control }) as StudyFormValues;

  useEffect(() => {
    beginNewStudy();
  }, [beginNewStudy]);

  useEffect(() => {
    let cancelled = false;
    api
      .species()
      .then((species) => {
        if (cancelled) {
          return;
        }
        setRegistry(species);
        const initial =
          species.find((item) => item.species === DEFAULTS.species) ??
          species[0];
        if (initial && !formState.isDirty) {
          reset({
            ...getValues(),
            species: initial.species,
            assembly: initial.canonical_assembly,
            window_kb: defaultWindowKb(initial),
          });
        }
      })
      .catch((error: unknown) => {
        if (isUnauthorizedStatus(error)) {
          clearApiKey("rejected");
          return;
        }
        if (!cancelled) {
          setRegistryError(
            "The species registry could not be loaded; species and assembly checks are unavailable.",
          );
        }
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api]);

  const species = registry?.find((item) => item.species === values.species);
  const assembly = species?.assemblies.find(
    (item) => item.id === values.assembly,
  );
  const parseOptions = useMemo<ParseOptions>(
    () => ({
      assembly: assembly
        ? { id: assembly.id, chromosomes: assembly.chromosomes }
        : null,
      prefixes: species?.chromosome_prefixes ?? [],
    }),
    [assembly, species],
  );

  const onParsed = useCallback(
    (result: ParseResult | null) => {
      setParsed(result);
      setValue("snps", result ? submittableSnps(result.rows) : [], {
        shouldValidate: formState.isSubmitted,
        shouldDirty: true,
      });
      const key = result?.preset === "sister-team" ? parsedKey(result) : null;
      if (key !== presetSeen.current) {
        presetSeen.current = key;
        setValue("assembly_confirmed", key === null, {
          shouldValidate: formState.isSubmitted,
        });
      }
    },
    [formState.isSubmitted, setValue],
  );

  const dryRun = useMemo(() => {
    if (values.mode === "snps" && !(values.snps?.length ?? 0)) {
      return null;
    }
    return toStudyRequest({
      ...values,
      trait_text: values.trait_text?.trim() || "unspecified",
      window_kb: Number.isFinite(values.window_kb) ? values.window_kb : 0,
      ld_r2: Number.isFinite(values.ld_r2) ? values.ld_r2 : 0.2,
    });
  }, [values]);
  const dryRunKey = dryRun ? JSON.stringify(dryRun) : null;

  useEffect(() => {
    if (!dryRunKey) {
      return;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => {
      setValidating(true);
      api
        .validate(JSON.parse(dryRunKey), controller.signal)
        .then(setValidation)
        .catch((error: unknown) => {
          if (isUnauthorizedStatus(error)) {
            clearApiKey("rejected");
          }
        })
        .finally(() => {
          if (!controller.signal.aborted) {
            setValidating(false);
          }
        });
    }, 500);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [api, clearApiKey, dryRunKey]);

  const currentValidation = dryRunKey ? validation : null;
  const previewRows = useMemo(
    () => mergeValidation(parsed?.rows ?? [], currentValidation),
    [parsed, currentValidation],
  );

  const ldLimit = ldWarningKb(species);
  const ldReady = ldAvailable(species);
  const wideWindow =
    values.window_mode === "fixed" &&
    ldLimit !== null &&
    values.window_kb > ldLimit;
  const suggestions = TRAIT_SUGGESTIONS[values.species] ?? [];

  const onSubmit = form.handleSubmit((submitted) => {
    const study = toStudyRequest(submitted);
    const metadata: StudyMetadata = {
      kind: "study",
      assistant_id: STUDY_ASSISTANT_ID,
      graph_id: STUDY_ASSISTANT_ID,
      species: study.species,
      assembly: study.assembly,
      trait: study.trait_text,
      n_snps: study.mode === "snps" ? study.snps.length : 0,
      mode: study.mode,
    };
    setSubmitting(true);
    useRunStore.getState().reset();
    stream
      .submit(
        {
          study,
          messages: [{ type: "human", content: studySummary(study) }],
        },
        {
          streamMode: ["values", "custom"],
          streamSubgraphs: true,
          onDisconnect: "continue",
          metadata,
        },
      )
      .catch(() =>
        toast.error("The study could not be started.", {
          description: "Check the server connection and try again.",
        }),
      )
      .finally(() => setSubmitting(false));
  });

  return (
    <form
      onSubmit={onSubmit}
      noValidate
      aria-label="New study"
      className="grid gap-6 xl:grid-cols-[minmax(0,1fr)_minmax(0,1.1fr)]"
    >
      <div className="flex flex-col gap-6">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">New study</h1>
          <p className="text-muted-foreground text-sm">
            Place GWAS SNPs on a reference assembly, build loci around them and
            let the specialists rank the candidate genes.
          </p>
        </div>
        {registryError && (
          <p
            role="alert"
            className="text-sm text-amber-700"
          >
            {registryError}
          </p>
        )}
        <Card>
          <CardHeader>
            <CardTitle>Study</CardTitle>
            <CardDescription>
              Start from a SNP list, or from a species and trait.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-5">
            <Controller
              control={control}
              name="mode"
              render={({ field }) => (
                <RadioGroup
                  value={field.value}
                  onValueChange={field.onChange}
                  aria-label="Study input"
                  className="grid grid-cols-2 gap-2"
                >
                  {[
                    {
                      value: "snps",
                      label: "SNP list",
                      hint: "Paste or upload GWAS hits",
                    },
                    {
                      value: "trait",
                      label: "Species + trait",
                      hint: "The model step proposes SNPs",
                    },
                  ].map((option) => (
                    <Label
                      key={option.value}
                      htmlFor={`${ids}-mode-${option.value}`}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-md border p-3",
                        field.value === option.value &&
                          "border-foreground/40 bg-muted/50",
                      )}
                    >
                      <RadioGroupItem
                        id={`${ids}-mode-${option.value}`}
                        value={option.value}
                        className="mt-0.5"
                      />
                      <span className="flex flex-col gap-0.5">
                        <span className="font-medium">{option.label}</span>
                        <span className="text-muted-foreground text-xs font-normal">
                          {option.hint}
                        </span>
                      </span>
                    </Label>
                  ))}
                </RadioGroup>
              )}
            />
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="flex flex-col gap-2">
                <Label htmlFor={`${ids}-species`}>Species</Label>
                <Controller
                  control={control}
                  name="species"
                  render={({ field }) => (
                    <select
                      id={`${ids}-species`}
                      className="border-input bg-background h-9 rounded-md border px-3 text-sm capitalize"
                      value={field.value}
                      onBlur={field.onBlur}
                      aria-invalid={!!formState.errors.species || undefined}
                      onChange={(event) => {
                        field.onChange(event.target.value);
                        const next = registry?.find(
                          (item) => item.species === event.target.value,
                        );
                        if (next) {
                          setValue("assembly", next.canonical_assembly, {
                            shouldValidate: true,
                          });
                          setValue("window_kb", defaultWindowKb(next), {
                            shouldValidate: true,
                          });
                        }
                      }}
                    >
                      {(
                        registry ?? [{ species: field.value } as SpeciesInfo]
                      ).map((item) => (
                        <option
                          key={item.species}
                          value={item.species}
                        >
                          {item.common_name ?? item.species}
                          {item.scientific_name
                            ? ` (${item.scientific_name})`
                            : ""}
                          {registry && !item.bundle ? " — no data bundle" : ""}
                        </option>
                      ))}
                    </select>
                  )}
                />
                <FieldError
                  id={`${ids}-species-error`}
                  message={formState.errors.species?.message}
                />
              </div>
              <div className="flex flex-col gap-2">
                <Label htmlFor={`${ids}-assembly`}>Assembly</Label>
                <Controller
                  control={control}
                  name="assembly"
                  render={({ field }) => (
                    <select
                      id={`${ids}-assembly`}
                      className="border-input bg-background h-9 rounded-md border px-3 text-sm"
                      value={field.value}
                      onBlur={field.onBlur}
                      onChange={(event) => {
                        field.onChange(event.target.value);
                        setValue("assembly_confirmed", true, {
                          shouldValidate: formState.isSubmitted,
                        });
                      }}
                      aria-invalid={!!formState.errors.assembly || undefined}
                    >
                      {(species?.assemblies ?? [{ id: field.value }]).map(
                        (item) => (
                          <option
                            key={item.id}
                            value={item.id}
                          >
                            {item.id}
                            {"canonical" in item && item.canonical
                              ? " (canonical)"
                              : ""}
                            {"lift_to" in item && item.lift_to
                              ? ` (lifted to ${item.lift_to})`
                              : ""}
                          </option>
                        ),
                      )}
                    </select>
                  )}
                />
                <FieldError
                  id={`${ids}-assembly-error`}
                  message={formState.errors.assembly?.message}
                />
              </div>
            </div>
            {registry && species && !species.bundle && (
              <p className="flex items-start gap-2 text-sm text-amber-700">
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />
                No data bundle is built for {species.common_name}; the study
                will stop when it builds loci.
              </p>
            )}
            {assembly?.description && (
              <p className="text-muted-foreground text-xs">
                {assembly.description}
              </p>
            )}
            <div className="flex flex-col gap-2">
              <Label htmlFor={`${ids}-trait`}>Trait</Label>
              <Controller
                control={control}
                name="trait_text"
                render={({ field }) => (
                  <TraitField
                    id={`${ids}-trait`}
                    value={field.value ?? ""}
                    onChange={field.onChange}
                    onBlur={field.onBlur}
                    suggestions={suggestions}
                    invalid={!!formState.errors.trait_text}
                    describedBy={
                      formState.errors.trait_text
                        ? `${ids}-trait-error`
                        : undefined
                    }
                  />
                )}
              />
              <FieldError
                id={`${ids}-trait-error`}
                message={formState.errors.trait_text?.message}
              />
            </div>
          </CardContent>
        </Card>

        {values.mode === "snps" ? (
          <Card>
            <CardHeader>
              <CardTitle>SNPs</CardTitle>
              <CardDescription>
                Chromosome aliases are normalized against {values.assembly};
                marker ids are placed by the server.
              </CardDescription>
            </CardHeader>
            <CardContent className="flex flex-col gap-3">
              <SnpInput
                options={parseOptions}
                onParsed={onParsed}
                invalid={!!formState.errors.snps}
              />
              {parsed?.preset === "sister-team" && species && (
                <SisterTeamAssembly
                  species={species}
                  rows={parsed.rows}
                  value={values.assembly}
                  confirmed={values.assembly_confirmed}
                  onChoose={(id) => {
                    setValue("assembly", id, { shouldValidate: true });
                    setValue("assembly_confirmed", true, {
                      shouldValidate: true,
                    });
                  }}
                />
              )}
              <FieldError
                id={`${ids}-snps-error`}
                message={formState.errors.snps?.message}
              />
            </CardContent>
          </Card>
        ) : (
          <Card>
            <CardContent className="flex items-start gap-3 text-sm">
              <Info className="mt-0.5 size-4 shrink-0 text-sky-700" />
              <p>
                <span className="font-medium">Model step.</span> No trained
                model or precomputed association results are registered for{" "}
                {species?.common_name ?? values.species} yet. The model agent is
                a stub that proposes placeholder SNPs, and the report marks them
                as such.
              </p>
            </CardContent>
          </Card>
        )}

        <Card>
          <CardHeader>
            <CardTitle>Window</CardTitle>
            <CardDescription>
              Each SNP becomes a fixed ± window or its LD window; overlapping
              windows merge into one locus.
            </CardDescription>
          </CardHeader>
          <CardContent className="flex flex-col gap-3">
            <Controller
              control={control}
              name="window_mode"
              render={({ field }) => (
                <RadioGroup
                  value={field.value}
                  onValueChange={field.onChange}
                  aria-label="Window mode"
                  className="grid grid-cols-2 gap-2"
                >
                  {[
                    {
                      value: "fixed",
                      label: "Fixed",
                      hint: "SNP ± the flank below",
                      disabled: false,
                    },
                    {
                      value: "ld",
                      label: "LD",
                      hint: ldReady
                        ? `Furthest SNP in LD (${species?.ld?.panels.join(", ")} panel)`
                        : (species?.ld?.reason ?? "No LD panel in this build"),
                      disabled: !ldReady,
                    },
                  ].map((option) => (
                    <Label
                      key={option.value}
                      htmlFor={`${ids}-window-mode-${option.value}`}
                      className={cn(
                        "flex cursor-pointer items-start gap-3 rounded-md border p-3",
                        field.value === option.value &&
                          "border-foreground/40 bg-muted/50",
                        option.disabled && "cursor-not-allowed opacity-60",
                      )}
                    >
                      <RadioGroupItem
                        id={`${ids}-window-mode-${option.value}`}
                        value={option.value}
                        disabled={option.disabled}
                        className="mt-0.5"
                      />
                      <span className="flex flex-col gap-0.5">
                        <span className="font-medium">{option.label}</span>
                        <span className="text-muted-foreground text-xs font-normal">
                          {option.hint}
                        </span>
                      </span>
                    </Label>
                  ))}
                </RadioGroup>
              )}
            />
            <FieldError
              id={`${ids}-window-mode-error`}
              message={formState.errors.window_mode?.message}
            />
            {values.window_mode === "ld" && (
              <Controller
                control={control}
                name="ld_r2"
                render={({ field }) => (
                  <div className="flex items-center gap-2">
                    <Label htmlFor={`${ids}-ld-r2`}>Minimum r²</Label>
                    <Input
                      id={`${ids}-ld-r2`}
                      type="number"
                      inputMode="decimal"
                      min={0.05}
                      max={1}
                      step={0.05}
                      className="w-24"
                      value={Number.isFinite(field.value) ? field.value : ""}
                      onBlur={field.onBlur}
                      onChange={(event) =>
                        field.onChange(
                          event.target.value === ""
                            ? Number.NaN
                            : Number(event.target.value),
                        )
                      }
                      aria-invalid={!!formState.errors.ld_r2 || undefined}
                    />
                    <span className="text-muted-foreground text-xs">
                      The fixed flank below is used where LD cannot be computed.
                    </span>
                  </div>
                )}
              />
            )}
            <FieldError
              id={`${ids}-ld-r2-error`}
              message={formState.errors.ld_r2?.message}
            />
            <Controller
              control={control}
              name="window_kb"
              render={({ field }) => (
                <div className="flex items-center gap-4">
                  <Slider
                    aria-label="Window size in kb"
                    min={10}
                    max={1000}
                    step={10}
                    value={[Math.min(Math.max(field.value || 10, 10), 1000)]}
                    onValueChange={([next]) => field.onChange(next)}
                    className="flex-1"
                  />
                  <div className="flex items-center gap-1">
                    <Label
                      htmlFor={`${ids}-window`}
                      className="sr-only"
                    >
                      Window size (kb)
                    </Label>
                    <span className="text-muted-foreground text-sm">±</span>
                    <Input
                      id={`${ids}-window`}
                      type="number"
                      inputMode="numeric"
                      min={1}
                      max={5000}
                      className="w-24"
                      value={Number.isFinite(field.value) ? field.value : ""}
                      onBlur={field.onBlur}
                      onChange={(event) =>
                        field.onChange(
                          event.target.value === ""
                            ? Number.NaN
                            : Number(event.target.value),
                        )
                      }
                      aria-invalid={!!formState.errors.window_kb || undefined}
                    />
                    <span className="text-muted-foreground text-sm">kb</span>
                  </div>
                </div>
              )}
            />
            <FieldError
              id={`${ids}-window-error`}
              message={formState.errors.window_kb?.message}
            />
            {species && (
              <p className="text-muted-foreground text-xs">
                Default for {species.common_name}: ±{defaultWindowKb(species)}{" "}
                kb. {species.ld_note}
              </p>
            )}
            {wideWindow && species && (
              <p
                role="status"
                className="flex items-start gap-2 text-sm text-amber-700"
              >
                <AlertTriangle className="mt-0.5 size-4 shrink-0" />±
                {values.window_kb} kb is more than twice the typical{" "}
                {species.common_name} LD of {species.typical_ld_kb} kb; loci
                will hold many genes unrelated to the SNP.
              </p>
            )}
          </CardContent>
        </Card>

        <Accordion
          type="single"
          collapsible
          className="bg-card rounded-xl border px-4"
        >
          <AccordionItem value="advanced">
            <AccordionTrigger>Advanced</AccordionTrigger>
            <AccordionContent className="flex flex-col gap-4">
              <fieldset className="flex flex-col gap-2">
                <legend className="mb-1 text-sm font-medium">
                  Specialists
                </legend>
                <Controller
                  control={control}
                  name="specialists_enabled"
                  render={({ field }) => (
                    <div className="grid gap-2 sm:grid-cols-2">
                      {SPECIALISTS.map((name) => (
                        <Label
                          key={name}
                          htmlFor={`${ids}-specialist-${name}`}
                          className="flex items-center gap-2 font-normal"
                        >
                          <Checkbox
                            id={`${ids}-specialist-${name}`}
                            checked={field.value.includes(name)}
                            onCheckedChange={(checked) =>
                              field.onChange(
                                checked
                                  ? SPECIALISTS.filter(
                                      (item) =>
                                        item === name ||
                                        field.value.includes(item),
                                    )
                                  : field.value.filter((item) => item !== name),
                              )
                            }
                          />
                          {SPECIALIST_LABELS[name]}
                        </Label>
                      ))}
                    </div>
                  )}
                />
                <FieldError
                  id={`${ids}-specialists-error`}
                  message={formState.errors.specialists_enabled?.message}
                />
              </fieldset>
              <div className="flex items-center gap-3">
                <Label htmlFor={`${ids}-topk`}>Top genes per locus</Label>
                <Controller
                  control={control}
                  name="top_k_per_locus"
                  render={({ field }) => (
                    <Input
                      id={`${ids}-topk`}
                      type="number"
                      min={1}
                      max={50}
                      className="w-20"
                      value={field.value}
                      onBlur={field.onBlur}
                      onChange={(event) =>
                        field.onChange(Number(event.target.value))
                      }
                    />
                  )}
                />
              </div>
            </AccordionContent>
          </AccordionItem>
        </Accordion>

        <div className="flex items-center justify-end gap-3">
          {Object.keys(formState.errors).length > 0 && (
            <p className="text-sm text-rose-600">
              Fix the highlighted fields to start the study.
            </p>
          )}
          <Button
            type="submit"
            size="lg"
            variant="brand"
            disabled={submitting}
          >
            {submitting ? <Loader2 className="animate-spin" /> : <Play />}
            {submitting ? "Starting" : "Start study"}
          </Button>
        </div>
      </div>

      <aside className="flex flex-col gap-4 xl:sticky xl:top-20 xl:self-start">
        <h2 className="text-lg font-semibold tracking-tight">Preview</h2>
        <ServerPreview
          validation={currentValidation}
          windowKb={values.window_kb}
        />
        {values.mode === "snps" ? (
          <SnpPreview
            rows={previewRows}
            validation={currentValidation}
            validating={validating}
          />
        ) : (
          <p className="text-muted-foreground text-sm">
            Trait studies have no SNPs to preview until the model step runs.
          </p>
        )}
      </aside>
    </form>
  );
}
