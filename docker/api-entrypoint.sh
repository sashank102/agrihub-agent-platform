#!/bin/sh
set -eu

workers="${WEB_CONCURRENCY:-1}"
if [ "$workers" != "1" ]; then
  echo "AgriHub requires exactly one API worker." >&2
  exit 1
fi

python -m agent_platform.bootstrap
exec python -c "from agent_platform.main import run; run()"
