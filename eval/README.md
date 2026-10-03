# Evaluation harness

`soybean_benchmark.py` runs the known-gene retrospective benchmark on the built
soybean bundle (`agrihub.benchmark`) and writes a scorecard to `results/`:

```bash
./.tools/bin/uv run python eval/soybean_benchmark.py
./.tools/bin/uv run python eval/soybean_benchmark.py --traits "plant height" "maturity" --max-loci 4 --no-smoke
```

How a pseudo-study is built:

- For each benchmark trait, the curated trait genes (LIS `glyma.traits.yml`,
  the bundle's `known_genes`) that have a trait-matched GWAS Atlas hit within
  the typical soybean LD distance (150 kb). The most significant hit is the
  study SNP; the curated gene is the target of its locus. Targets are at
  least two windows apart, at most `--max-loci` per trait.
- While a study runs, the target's curated record, every catalog GWAS hit that
  reports the target, and the input hit are hidden from the tools, so the
  rubric is not graded on evidence it was given.

What the scorecard reports:

- recall@1/3/5 and the median rank of the target within its locus for three
  rankings of the same genes: distance to the nearest SNP, the deterministic
  rubric with no specialist rounds, and the full agent pipeline;
- the verifier's verdicts on the top candidates' claims: unsupported (no
  independent source) and contradicted rates;
- how many cited evidence aliases resolve in the run's evidence store;
- runtime and input/output tokens per run;
- the poster SNPs as a smoke case (`Glyma.18G092200` in the `S18_9263941` locus).

The agents use a real model when `ANTHROPIC_API_KEY` or `OPENAI_API_KEY` is set
(`AGRIHUB_EVAL_MODEL` picks it) and the scripted `agrihub-fake:poster` model
otherwise; the scorecard says which. Only the small Markdown and CSV
scorecards are committed; evidence stores go to a temporary directory.
