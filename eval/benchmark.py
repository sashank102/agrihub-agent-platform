"""Run the known-gene benchmark for one species and write a Markdown and CSV scorecard to eval/results/.

Usage (from agent-platform/)::

    ./.tools/bin/uv run python eval/benchmark.py --species rice
    ./.tools/bin/uv run python eval/benchmark.py --species maize --traits "plant height" --max-loci 2

The agents use ``AGRIHUB_EVAL_MODEL`` when ANTHROPIC_API_KEY or OPENAI_API_KEY
is set, and the scripted ``agrihub-fake:poster`` model otherwise; the
scorecard names the model it ran with and marks scripted agent results as
pending a real model. Runs write their evidence stores to a temporary
directory, never to ``var/runs``.
"""

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from agrihub.benchmark import SPECIES_BENCHMARKS, run_benchmark
from agrihub.configuration import DEFAULT_MODEL
from agrihub_data.bundle import open_bundle

RESULTS = Path(__file__).resolve().parent / "results"
FAKE = "agrihub-fake:poster"


def pick_model() -> str:
    """Return the model the agents run with: a real provider when a key is set, else the scripted model."""
    if os.environ.get("ANTHROPIC_API_KEY"):
        return os.environ.get("AGRIHUB_EVAL_MODEL", DEFAULT_MODEL)
    if os.environ.get("OPENAI_API_KEY"):
        return os.environ.get("AGRIHUB_EVAL_MODEL", "openai:gpt-4.1")
    return FAKE


def main(argv: list[str] | None = None, *, species: str | None = None) -> int:
    """Run the benchmark and write the scorecard."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    if species is None:
        parser.add_argument("--species", required=True, choices=sorted(SPECIES_BENCHMARKS))
    parser.add_argument("--traits", nargs="*", default=None, help="the species' benchmark traits when omitted")
    parser.add_argument("--max-loci", type=int, default=8)
    parser.add_argument("--flank-kb", type=int, help="window flank; the species default when omitted")
    parser.add_argument("--no-smoke", action="store_true", help="skip the soybean poster smoke case")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--name", default=None, help="file stem; <species>-<date> by default")
    args = parser.parse_args(argv)
    chosen = species or args.species

    model = pick_model()
    os.environ["MODEL"] = model
    for role in ("ORCHESTRATOR_MODEL", "SPECIALIST_MODEL", "VERIFIER_MODEL", "WRITER_MODEL", "QA_MODEL"):
        os.environ.pop(role, None)
    with tempfile.TemporaryDirectory(prefix="agrihub-eval-") as runs:
        os.environ["AGRIHUB_RUN_DIR"] = runs
        scorecard = run_benchmark(
            open_bundle(chosen),
            traits=tuple(args.traits) if args.traits else None,
            max_loci=args.max_loci,
            flank_bp=args.flank_kb * 1_000 if args.flank_kb else None,
            model=model,
            smoke=not args.no_smoke,
            progress=lambda message: print(message, file=sys.stderr, flush=True),
        )
    if model == FAKE:
        scorecard.notes.append(
            "No ANTHROPIC_API_KEY or OPENAI_API_KEY was set, so the agents ran on the scripted fake model; "
            "the real-model run is pending."
        )
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.name or f"{chosen}-{datetime.now(UTC).date().isoformat()}"
    (args.out / f"{stem}.md").write_text(scorecard.markdown(), encoding="utf-8")
    (args.out / f"{stem}.csv").write_text(scorecard.csv_text(), encoding="utf-8")
    print(scorecard.markdown())
    print(f"wrote {args.out / stem}.md and .csv", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
