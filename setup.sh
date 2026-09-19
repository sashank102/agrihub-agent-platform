#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

python -m pip install --prefix "$ROOT_DIR/.tools" uv

export UV_PYTHON_INSTALL_DIR="$ROOT_DIR/.uv-python"
export UV_PYTHON_BIN_DIR="$ROOT_DIR/.uv-python/bin"
export UV_CACHE_DIR="$ROOT_DIR/.uv-cache"

cd "$ROOT_DIR"
"$ROOT_DIR/.tools/bin/uv" python install 3.11
"$ROOT_DIR/.tools/bin/uv" sync --python 3.11

cd "$ROOT_DIR/frontend"
npx --yes pnpm@10.5.1 install --frozen-lockfile

echo
echo "Setup complete."
echo "1. Put your Anthropic key in $ROOT_DIR/.env"
echo "2. Run $ROOT_DIR/start-dev.sh"
