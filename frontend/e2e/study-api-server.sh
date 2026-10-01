#!/usr/bin/env bash
# Serve the API for the study browser test. Playwright passes the settings
# (database, auth, port, CORS origin) and stops the server when the run ends.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
exec ./.tools/bin/uv run python -c 'from agent_platform.main import run; run()'
