#!/usr/bin/env bash
# Restart the API when frontend/e2e/.restart appears. A crash without that
# flag ends the process so Playwright reports the failure.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
FLAG="$ROOT/frontend/e2e/.restart"
rm -f "$FLAG"

while true; do
  ./.tools/bin/uv run python -c 'from agent_platform.main import run; run()' &
  pid=$!
  restarted=0
  while kill -0 "$pid" 2>/dev/null; do
    if [[ -f "$FLAG" ]]; then
      rm -f "$FLAG"
      kill "$pid" 2>/dev/null || true
      wait "$pid" 2>/dev/null || true
      restarted=1
      break
    fi
    sleep 0.2
  done
  if [[ "$restarted" -eq 0 ]]; then
    wait "$pid" 2>/dev/null || true
    exit 1
  fi
done
