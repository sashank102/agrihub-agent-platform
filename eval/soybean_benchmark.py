"""Run the soybean known-gene benchmark; ``eval/benchmark.py --species soybean`` with the poster smoke case.

Usage (from agent-platform/)::

    ./.tools/bin/uv run python eval/soybean_benchmark.py            # every benchmark trait
    ./.tools/bin/uv run python eval/soybean_benchmark.py --traits "plant height" --max-loci 2 --no-smoke
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from benchmark import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main(species="soybean"))
