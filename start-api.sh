#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="$ROOT_DIR/.tools/bin/uv"

if [[ ! -x "$UV" ]]; then
  echo "Dependencies are missing. Run ./setup.sh first." >&2
  exit 1
fi

cd "$ROOT_DIR"
exec "$UV" run python -c "from agent_platform.main import run; run()"
