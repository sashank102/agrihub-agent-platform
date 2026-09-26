#!/usr/bin/env bash
# Start the single-node PostgreSQL, FastAPI, and Next.js stack.
# Local development uses ./start-dev.sh instead. This script does not print secrets.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

if [[ -f "$ROOT_DIR/.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT_DIR/.env"
  set +a
fi

fail() {
  echo "$1" >&2
  exit 1
}

if ! command -v docker >/dev/null 2>&1; then
  fail "Docker is required."
fi
if ! docker compose version >/dev/null 2>&1; then
  fail "Docker Compose is required."
fi

export ENVIRONMENT=production
export AUTH_MODE=api_key
export POSTGRES_DB="${POSTGRES_DB:-agent_platform}"
export POSTGRES_USER="${POSTGRES_USER:-agent_platform}"
export POSTGRES_PORT="${POSTGRES_PORT:-5432}"
export API_PORT="${API_PORT:-8000}"
export FRONTEND_PORT="${FRONTEND_PORT:-3000}"
export NEXT_PUBLIC_API_URL="${NEXT_PUBLIC_API_URL:-http://127.0.0.1:${API_PORT}}"
export NEXT_PUBLIC_ASSISTANT_ID="${NEXT_PUBLIC_ASSISTANT_ID:-agrihub}"
export API_ALLOWED_ORIGINS="${API_ALLOWED_ORIGINS:-[\"http://127.0.0.1:${FRONTEND_PORT}\",\"http://localhost:${FRONTEND_PORT}\"]}"
export WEB_CONCURRENCY=1
export GRAPH_FIXTURE="${GRAPH_FIXTURE:-false}"
export MODEL="${MODEL:-anthropic:claude-sonnet-4-20250514}"
export SEARCH_API="${SEARCH_API:-anthropic}"

if [[ -z "${POSTGRES_PASSWORD:-}" || ${#POSTGRES_PASSWORD} -lt 16 ]]; then
  fail "POSTGRES_PASSWORD is required and must be at least 16 characters."
fi
if [[ -z "${API_KEY_PEPPER:-}" || ${#API_KEY_PEPPER} -lt 16 ]]; then
  fail "API_KEY_PEPPER is required and must be at least 16 characters."
fi
if [[ "${POSTGRES_PASSWORD}" == "agent_platform" || "${API_KEY_PEPPER}" == "agent_platform" ]]; then
  fail "Refusing the example password or pepper."
fi

if [[ "$GRAPH_FIXTURE" != "true" ]]; then
  model_provider="${MODEL%%:*}"
  case "$model_provider" in
    google | google_genai | google_vertexai) provider_key="${GOOGLE_API_KEY:-}" ;;
    bedrock)
      if [[ -z "${AWS_ACCESS_KEY_ID:-}" && -z "${AWS_PROFILE:-}" ]]; then
        fail "Bedrock requires AWS_ACCESS_KEY_ID or AWS_PROFILE."
      fi
      provider_key="aws"
      ;;
    *)
      provider_variable="${model_provider^^}_API_KEY"
      provider_variable="${provider_variable//-/_}"
      provider_key="${!provider_variable:-}"
      ;;
  esac
  if [[ -z "$provider_key" ]]; then
    fail "The configured MODEL provider '$model_provider' requires its server-side API key."
  fi
  if [[ "${SEARCH_API:-anthropic}" == "tavily" && -z "${TAVILY_API_KEY:-}" ]]; then
    fail "SEARCH_API=tavily requires TAVILY_API_KEY."
  fi
fi

echo "Building and starting PostgreSQL, the API, and the frontend."
docker compose up --build -d
docker compose up --wait

echo "AgriHub UI:  http://127.0.0.1:${FRONTEND_PORT}"
echo "AgriHub API: http://127.0.0.1:${API_PORT}"
echo "Health:      http://127.0.0.1:${API_PORT}/health"
echo "Readiness:   http://127.0.0.1:${API_PORT}/ready"
echo
echo "Bootstrap the first key from a shell that already has DATABASE_URI and API_KEY_PEPPER:"
echo "  docker compose exec api python -m agent_platform create-user --display-name \"Operator\""
echo "  docker compose exec api python -m agent_platform issue-key --user-id <user-id> --label bootstrap"
echo
echo "Stop:    docker compose down"
echo "Status:  docker compose ps"
echo "Logs:    docker compose logs --tail 100"
echo "The plaintext API key is printed once by issue-key. This script does not print it."
