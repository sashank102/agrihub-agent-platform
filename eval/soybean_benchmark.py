"""Run the soybean known-gene benchmark and write a Markdown and CSV scorecard to eval/results/.

Usage (from agent-platform/)::

    ./.tools/bin/uv run python eval/soybean_benchmark.py            # every benchmark trait
    ./.tools/bin/uv run python eval/soybean_benchmark.py --traits "plant height" --max-loci 2 --no-smoke

The agents use ``AGRIHUB_EVAL_MODEL`` when ANTHROPIC_API_KEY or OPENAI_API_KEY
is set, and the scripted ``agrihub-fake:poster`` model otherwise; the
scorecard names the model it ran with. Runs write their evidence stores to a
temporary directory, never to ``var/runs``.
"""

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from agrihub.benchmark import SOYBEAN_TRAITS, run_benchmark
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


def main(argv: list[str] | None = None) -> int:
    """Run the benchmark and write the scorecard."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--traits", nargs="*", default=list(SOYBEAN_TRAITS))
    parser.add_argument("--max-loci", type=int, default=8)
    parser.add_argument("--flank-kb", type=int, help="window flank; the species default when omitted")
    parser.add_argument("--no-smoke", action="store_true", help="skip the poster smoke case")
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--name", default=None, help="file stem; soybean-<date> by default")
    args = parser.parse_args(argv)

    model = pick_model()
    os.environ["MODEL"] = model
    for role in ("ORCHESTRATOR_MODEL", "SPECIALIST_MODEL", "VERIFIER_MODEL", "WRITER_MODEL", "QA_MODEL"):
        os.environ.pop(role, None)
    with tempfile.TemporaryDirectory(prefix="agrihub-eval-") as runs:
        os.environ["AGRIHUB_RUN_DIR"] = runs
        scorecard = run_benchmark(
            open_bundle("soybean"),
            traits=tuple(args.traits),
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
    stem = args.name or f"soybean-{datetime.now(UTC).date().isoformat()}"
    (args.out / f"{stem}.md").write_text(scorecard.markdown(), encoding="utf-8")
    (args.out / f"{stem}.csv").write_text(scorecard.csv_text(), encoding="utf-8")
    print(scorecard.markdown())
    print(f"wrote {args.out / stem}.md and .csv", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
