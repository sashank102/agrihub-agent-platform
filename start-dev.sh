#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="$ROOT_DIR/.tools/bin/uv"
NEXT="$ROOT_DIR/frontend/node_modules/.bin/next"

if [[ ! -x "$UV" || ! -x "$NEXT" ]]; then
  echo "Dependencies are missing. Run ./setup.sh first." >&2
  exit 1
fi

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

export UV_PYTHON_INSTALL_DIR="$ROOT_DIR/.uv-python"
export UV_PYTHON_BIN_DIR="$ROOT_DIR/.uv-python/bin"
export UV_CACHE_DIR="$ROOT_DIR/.uv-cache"
export DATABASE_URI="${DATABASE_URI:-postgresql://agent_platform:agent_platform@localhost:5432/agent_platform}"
export AUTH_MODE="${AUTH_MODE:-api_key}"

if [[ "$AUTH_MODE" == "api_key" ]]; then
  if [[ -z "${API_KEY_PEPPER:-}" || ${#API_KEY_PEPPER} -lt 16 ]]; then
    echo "AUTH_MODE=api_key requires API_KEY_PEPPER of at least 16 characters in .env." >&2
    echo "Then bootstrap a key and paste it into the UI:" >&2
    echo "  ./.tools/bin/uv run python -m agent_platform create-user --display-name \"Local\"" >&2
    echo "  ./.tools/bin/uv run python -m agent_platform issue-key --user-id <user-id> --label local" >&2
    exit 1
  fi
elif [[ "$AUTH_MODE" == "disabled" ]]; then
  echo "AUTH_MODE=disabled: the API uses the development user and does not validate keys." >&2
  echo "The UI shows an explicit development-mode gate. Production refuses this mode." >&2
else
  echo "AUTH_MODE must be api_key or disabled." >&2
  exit 1
fi

"$UV" run python - <<'PY'
import os
import sys

from sqlalchemy import create_engine, text

uri = os.environ.get("DATABASE_URI")
if not uri:
    print("DATABASE_URI is required.", file=sys.stderr)
    sys.exit(1)
try:
    engine = create_engine(uri, pool_pre_ping=True)
    with engine.connect() as connection:
        connection.execute(text("SELECT 1"))
    engine.dispose()
except Exception:
    print("PostgreSQL is not ready. Start it with: docker compose up -d postgres", file=sys.stderr)
    sys.exit(1)
PY

(
  cd "$ROOT_DIR"
  "$UV" run alembic upgrade head
)

cleanup() {
  trap - INT TERM EXIT
  if [[ -n "${BACKEND_PID:-}" ]]; then
    kill -- "-${BACKEND_PID}" 2>/dev/null || kill "${BACKEND_PID}" 2>/dev/null || true
  fi
  if [[ -n "${FRONTEND_PID:-}" ]]; then
    kill -- "-${FRONTEND_PID}" 2>/dev/null || kill "${FRONTEND_PID}" 2>/dev/null || true
  fi
  wait "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(
  cd "$ROOT_DIR"
  exec setsid "$UV" run python -c "from agent_platform.main import run; run()"
) &
BACKEND_PID=$!

(
  cd "$ROOT_DIR/frontend"
  exec setsid "$NEXT" dev
) &
FRONTEND_PID=$!

echo "AgriHub UI:  http://127.0.0.1:3000"
echo "AgriHub API: http://127.0.0.1:8000"
echo "API docs:    http://127.0.0.1:8000/docs"
echo "Press Ctrl+C to stop both services."

wait -n "$BACKEND_PID" "$FRONTEND_PID"
status=$?
cleanup
exit "$status"
