# Evaluation harness

`benchmark.py` runs the known-gene retrospective benchmark on a built species
bundle (`agrihub.benchmark`) and writes a scorecard to `results/`;
`soybean_benchmark.py` is the soybean run with the poster smoke case:

```bash
./.tools/bin/uv run python eval/benchmark.py --species rice
./.tools/bin/uv run python eval/benchmark.py --species maize --traits "plant height" --max-loci 4
./.tools/bin/uv run python eval/soybean_benchmark.py
```

How a pseudo-study is built:

- For each benchmark trait, the curated trait genes of the species'
  benchmark sources (`SPECIES_BENCHMARKS`): LIS `glyma.traits.yml` for
  soybean, RAP-DB curated/agronomic genes and funRiceGenes for rice, MaizeGDB
  classical genes for maize, the Grant et al. 2023 Ma/Dw genes (and QTL Atlas
  major genes once an export is loaded) for sorghum. A gene is a target when
  a trait-matched GWAS Atlas hit lies within the species' typical LD distance
  (soybean and rice 150 kb, maize 10 kb, sorghum 50 kb). The most significant
  hit is the study SNP; targets are at least two windows apart, at most
  `--max-loci` per trait. A trait without a target is listed with the reason.
- While a study runs, every curated record of the target, every catalog GWAS
  hit that reports the target, and the input hit are hidden from the tools,
  so the rubric is not graded on evidence it was given.

What the scorecard reports:

- recall@1/3/5 with percentile bootstrap 95% intervals over targets, and the
  median rank of the target within its locus, for four rankings of the same
  genes: distance to the nearest SNP, the deterministic rubric with no
  specialist rounds, the same rubric without the trait profile's seed families
  and curated keywords (the priors ablation), and the full agent pipeline;
- the verifier's verdicts on the top candidates' claims: unsupported (no
  independent source) and contradicted rates;
- how many cited evidence aliases resolve in the run's evidence store;
- runtime and input/output tokens per run;
- for soybean, the poster SNPs as a smoke case (`Glyma.18G092200` in the
  `S18_9263941` locus).

The agents use a real model when `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set
(`AGRIHUB_EVAL_MODEL` picks it) and the scripted `agrihub-fake:poster` model
otherwise; the scorecard says which, and labels the agents row "pending real
model" when it is scripted. Only the small Markdown and CSV scorecards are
committed; evidence stores go to a temporary directory.

`smoke_studies.py` runs one plant-height study per species through the HTTP
API and checks that every cited alias resolves in the run's evidence store
(run it on the API host, with `AGRIHUB_API_KEY` set).

Reading the 2026-10-03 scorecards (scripted model):

- Soybean: the rubric beats distance only on recall@3 (55% vs 15%); without
  seed families and keyword priors it keeps 40%.
- Rice: neither beats the other (rubric 6%, distance 9% at recall@3). Rice
  curation is dense (RAP-DB, Oryzabase and funRiceGenes list about 20,000
  gene-trait records), so the held-out target competes with neighbouring
  curated genes that stay visible, often its own paralogs or the cloned gene
  of the same QTL (Hd3a beside RFT1, Gn1a beside D2), and those win on
  curated-gene credit. Several RAP-DB/funRiceGenes trait links are also broad
  (a gene with many TO terms matches several traits).
- Maize: loci are small (±50 kb, 1-10 kb LD), so distance alone ranks 67% of
  targets first and every method reaches 100% at recall@3. Targets are named
  loci matched by keyword, which is a weak definition of a trait gene. No
  target exists for grain yield or kernel weight.
- Sorghum: five targets; ortholog transfer from Arabidopsis works (PLAZA plus
  Compara) but the first TAIR phenotypes of PHYB do not mention flowering, so
  Ma3 gets no ortholog credit. With n = 5 the intervals span 0-60%.
