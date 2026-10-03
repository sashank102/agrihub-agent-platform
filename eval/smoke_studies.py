"""Run one smoke study per species through the HTTP API and check that every citation resolves.

Usage (from agent-platform/, with the API running and sharing ``AGRIHUB_RUN_DIR``)::

    AGRIHUB_API_KEY=... ./.tools/bin/uv run python eval/smoke_studies.py --api http://127.0.0.1:8020

Each study runs as the ``agrihub_study`` assistant on a new thread. The final
report's cited aliases (``[E12]`` markers and its citations list) are looked up
in the run's evidence store, which lives under the server's run directory, so
this script must run on the same host. The expected gene is the curated gene
the SNP sits in; the script reports its rank, not a pass/fail on it.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from agrihub.benchmark import POSTER_SNPS
from agrihub.evidence_store import EvidenceStore

RESULTS = Path(__file__).resolve().parent / "results"
_ALIAS = re.compile(r"\[(E\d+)\]")

SMOKE_STUDIES: dict[str, dict[str, Any]] = {
    "soybean": {
        "expected": "Glyma.18G092200",
        "study": {"mode": "snps", "species": "soybean", "assembly": "Wm82.a2.v1", "trait_text": "plant height", "snps": list(POSTER_SNPS)},
    },
    "rice": {
        "expected": "Os01g0883800",
        "study": {
            "mode": "snps",
            "species": "rice",
            "assembly": "IRGSP-1.0",
            "trait_text": "plant height",
            "snps": [{"raw": "sd1_snp", "chrom": "Chr1", "pos": 38_383_500}, {"raw": "chr06_snp", "chrom": "6", "pos": 9_338_000}],
        },
    },
    "maize": {
        "expected": "Zm00001eb054480",
        "study": {
            "mode": "snps",
            "species": "maize",
            "assembly": "Zm-B73-REFERENCE-NAM-5.0",
            "trait_text": "plant height",
            "snps": [{"raw": "d8_snp", "chrom": "1", "pos": 272_696_000}, {"raw": "br2_snp", "chrom": "chr1", "pos": 206_630_000}],
        },
    },
    "sorghum": {
        "expected": "SORBI_3007G163800",
        "study": {
            "mode": "snps",
            "species": "sorghum",
            "assembly": "Sorghum_bicolor_NCBIv3",
            "trait_text": "plant height",
            "snps": [{"raw": "dw3_snp", "chrom": "Chr07", "pos": 59_825_000}, {"raw": "dw1_snp", "chrom": "9", "pos": 57_040_000}],
        },
    },
}


def run_study(client: httpx.Client, study: dict[str, Any]) -> tuple[dict[str, Any], str, float]:
    """Run a study on a new thread; return its final state values, the run id and the wall time."""
    thread = client.post("/threads", json={"metadata": {"graph_id": "agrihub_study", "kind": "study"}})
    thread.raise_for_status()
    thread_id = thread.json()["thread_id"]
    started = time.monotonic()
    with client.stream(
        "POST",
        f"/threads/{thread_id}/runs/stream",
        json={"assistant_id": "agrihub_study", "input": {"study": study}, "stream_mode": ["values", "custom"], "stream_subgraphs": True},
        timeout=httpx.Timeout(900.0, connect=30.0),
    ) as response:
        response.raise_for_status()
        run_id = response.headers["x-run-id"]
        last = ""
        for line in response.iter_lines():
            if line.startswith("event:"):
                last = line.split(":", 1)[1].strip()
    seconds = time.monotonic() - started
    if last != "end":
        raise RuntimeError(f"stream ended on {last!r}, not end")
    state = client.get(f"/threads/{thread_id}/state")
    state.raise_for_status()
    return dict(state.json()["values"]), run_id, seconds


def check(species: str, values: dict[str, Any], run_id: str, seconds: float, expected: str) -> dict[str, Any]:
    """Return the smoke row: loci, candidates, the expected gene's rank and citation resolvability."""
    report = values.get("report") or {}
    cited = set(_ALIAS.findall(str(report.get("markdown") or "")))
    cited |= {str(item.get("alias")) for item in report.get("citations") or [] if item.get("alias")}
    store = EvidenceStore.for_run(run_id)
    try:
        known = {item.alias for item in store.get(sorted(cited))}
    finally:
        store.close()
    candidates = list(report.get("candidates_full") or [])
    target = next((row for row in candidates if row.get("gene_id") == expected), None)
    return {
        "species": species,
        "run_id": run_id,
        "seconds": round(seconds, 1),
        "loci": len(report.get("loci") or []),
        "candidates": len(candidates),
        "expected": expected,
        "expected_rank": target.get("rank_in_locus") if target else None,
        "expected_tier": target.get("tier") if target else None,
        "cited": len(cited),
        "resolvable": len(cited & known),
        "gaps": report.get("gaps") or report.get("unavailable") or [],
    }


def main(argv: list[str] | None = None) -> int:
    """Run the smoke studies and write a Markdown summary."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--api", default="http://127.0.0.1:8000")
    parser.add_argument("--species", nargs="*", default=list(SMOKE_STUDIES))
    parser.add_argument("--out", type=Path, default=RESULTS)
    parser.add_argument("--name", default=None)
    args = parser.parse_args(argv)
    key = os.environ.get("AGRIHUB_API_KEY")
    if not key:
        print("set AGRIHUB_API_KEY to a platform API key", file=sys.stderr)
        return 2
    rows = []
    with httpx.Client(base_url=args.api, headers={"Authorization": f"Bearer {key}"}, timeout=60.0) as client:
        for species in args.species:
            entry = SMOKE_STUDIES[species]
            print(f"{species}: running", file=sys.stderr, flush=True)
            values, run_id, seconds = run_study(client, entry["study"])
            rows.append(check(species, values, run_id, seconds, entry["expected"]))
            print(json.dumps(rows[-1]), file=sys.stderr, flush=True)
    lines = [
        "# API smoke studies",
        "",
        f"One plant-height study per species through `POST /threads/{{id}}/runs/stream` ({args.api}); "
        "citations are checked against each run's evidence store.",
        "",
        "| Species | Run | Seconds | Loci | Candidates | Expected gene | Rank in locus | Tier | Citations resolvable |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            f"| {row['species']} | `{row['run_id']}` | {row['seconds']} | {row['loci']} | {row['candidates']} | "
            f"{row['expected']} | {row['expected_rank']} | {row['expected_tier']} | {row['resolvable']}/{row['cited']} |"
        )
    args.out.mkdir(parents=True, exist_ok=True)
    stem = args.name or f"smoke-api-{datetime.now(UTC).date().isoformat()}"
    (args.out / f"{stem}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("\n".join(lines))
    return 0 if all(row["cited"] and row["resolvable"] == row["cited"] for row in rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
