#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
UV="$ROOT_DIR/.tools/bin/uv"
NEXT="$ROOT_DIR/frontend/node_modules/.bin/next"

if [[ ! -x "$UV" || ! -x "$NEXT" ]]; then
  echo "Dependencies are missing. Run ./setup.sh first." >&2
  exit 1
fi

if ! grep -Eq '^ANTHROPIC_API_KEY=.+$' "$ROOT_DIR/.env"; then
  echo "Warning: add ANTHROPIC_API_KEY to $ROOT_DIR/.env before running research."
fi

export UV_PYTHON_INSTALL_DIR="$ROOT_DIR/.uv-python"
export UV_PYTHON_BIN_DIR="$ROOT_DIR/.uv-python/bin"
export UV_CACHE_DIR="$ROOT_DIR/.uv-cache"

cleanup() {
  trap - INT TERM EXIT
  kill -- "-${BACKEND_PID:-}" "-${FRONTEND_PID:-}" 2>/dev/null || true
  wait "${BACKEND_PID:-}" "${FRONTEND_PID:-}" 2>/dev/null || true
}
trap cleanup INT TERM EXIT

(
  cd "$ROOT_DIR"
  exec setsid "$UV" run langgraph dev --allow-blocking --no-browser
) &
BACKEND_PID=$!

(
  cd "$ROOT_DIR/frontend"
  exec setsid "$NEXT" dev
) &
FRONTEND_PID=$!

echo "AgriHub UI:       http://localhost:3000"
echo "LangGraph API:    http://localhost:2024"
echo "LangGraph docs:   http://localhost:2024/docs"
echo "Press Ctrl+C to stop both services."

wait -n "$BACKEND_PID" "$FRONTEND_PID"
