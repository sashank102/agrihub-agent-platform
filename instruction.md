# Starting AgriHub from scratch

These steps take a fresh clone to a running app at <http://127.0.0.1:3000>.
Run every command from the repository root (the folder with `start-dev.sh`).

## 1. Prerequisites

- Linux or macOS with `bash`.
- Python 3 with `pip` (only used once, to install `uv`; the app runs on its own Python 3.11).
- Node.js 20 or newer (`npx` installs pnpm).
- Docker with the Compose plugin (for PostgreSQL).
- About 3 GB of free disk for the soybean core data bundle, 10 GB for all soybean tiers.
- An Anthropic or OpenAI API key for the agents.

## 2. Clone and install

```bash
git clone https://github.com/sashank102/agrihub-agent-platform.git
cd agrihub-agent-platform
./setup.sh
```

`setup.sh` installs `uv` into `.tools/`, Python 3.11 into `.uv-python/`, the
Python dependencies into `.venv/`, and the frontend dependencies into
`frontend/node_modules/`.

## 3. Configure `.env`

```bash
cp .env.example .env
```

Edit `.env` and set:

| Variable | Value |
| --- | --- |
| `ANTHROPIC_API_KEY` | Your Anthropic key (or set `OPENAI_API_KEY` instead). |
| `MODEL` | The model every agent uses, for example `anthropic:claude-haiku-4-5`. |
| `API_KEY_PEPPER` | Any random string of 16+ characters. |
| `POSTGRES_PASSWORD` | Any password; `DATABASE_URI` must use the same one. |

Generate a pepper with:

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

Optional settings:

- Per-role models override `MODEL`: `ORCHESTRATOR_MODEL`, `SPECIALIST_MODEL`,
  `VERIFIER_MODEL`, `WRITER_MODEL`, `QA_MODEL`.
- `MODEL_MAX_TOKENS` (default 4096) caps each model reply.
- `PROMPT_CACHING=false` turns off Anthropic prompt caching (on by default).
- `AGRIHUB_DATA_DIR` and `AGRIHUB_RUN_DIR` default to `var/data` and `var/runs`.

Never commit `.env`.

## 4. Start PostgreSQL

```bash
docker compose up -d postgres
docker compose ps postgres        # wait for "healthy"
./.tools/bin/uv run alembic upgrade head
```

## 5. Build a species data bundle

The agents read offline DuckDB bundles; a study needs the bundle for its
species. Start with the soybean core tier (about 2.3 GB of downloads):

```bash
./.tools/bin/uv run agrihub-data fetch  --species soybean --tier core
./.tools/bin/uv run agrihub-data build  --species soybean --tier core
./.tools/bin/uv run agrihub-data verify --species soybean
```

More evidence (expression atlases, co-expression and regulatory networks,
pathways) comes with the extended tier:

```bash
./.tools/bin/uv run agrihub-data fetch --species soybean --tier extended
./.tools/bin/uv run agrihub-data build --species soybean --tier extended
./.tools/bin/uv run agrihub-data verify --species soybean
```

The heavy tier (variant effects, LD, haplotypes) also needs PLINK2 and the
Ensembl VEP Docker image: run `scripts/setup_heavy_tools.sh`, then fetch and
build `--tier heavy`.

Rice, maize and sorghum use the same commands with `--species rice`,
`maize` or `sorghum`. `agrihub-data budget --species <name>` prints the
download size per tier, and `agrihub-data status --species <name>` shows what
is built. See `README.md` ("Species data bundles") for the two manual sorghum
downloads.

## 6. Create a user and an API key

```bash
./.tools/bin/uv run python -m agent_platform create-user --display-name "Local"
# copy "user_id" from the JSON it prints
./.tools/bin/uv run python -m agent_platform issue-key --user-id "<user_id>" --label local
# copy "api_key" from the JSON it prints; it is shown only once
```

## 7. Start the app

```bash
./start-dev.sh
```

It checks the database, applies migrations, and starts:

- the UI at <http://127.0.0.1:3000>
- the API at <http://127.0.0.1:8000> (docs at `/docs`)

Open the UI and paste the API key. Press Ctrl+C to stop both services.

## 8. Run a first study

1. Click **New study**.
2. Pick soybean, assembly Wm82.a2.v1, and type the trait (for example
   `plant height`).
3. Click **Use the poster SNPs**, or paste your own SNPs (one per line, such as
   `S19_45243000` or `Gm19:45243000`), or choose **trait** mode to have the
   model agent pick SNPs from trait models.
4. Click **Start study**. The run view shows the orchestrator, the parallel
   specialists and the verifier; when it finishes, the report opens on the
   written Summary. Use **Ask about this study** for follow-up questions and
   **Download** for JSON, CSV, Markdown or PDF.

## Stopping and restarting

```bash
# Ctrl+C in the start-dev.sh terminal, then:
docker compose stop postgres
```

Next time, only `docker compose up -d postgres` and `./start-dev.sh` are
needed. Restart `start-dev.sh` after changing `.env` or pulling new code.

## Troubleshooting

- **"Dependencies are missing. Run ./setup.sh first."**: run `./setup.sh`.
- **`setup.sh` fails with "externally-managed-environment"** (system Python
  forbids `pip`): install `uv` another way (for example your package manager),
  then run the rest of the setup by hand:

  ```bash
  mkdir -p .tools/bin && ln -sf "$(command -v uv)" .tools/bin/uv
  export UV_PYTHON_INSTALL_DIR="$PWD/.uv-python" UV_CACHE_DIR="$PWD/.uv-cache"
  ./.tools/bin/uv python install 3.11 && ./.tools/bin/uv sync --python 3.11
  (cd frontend && npx --yes pnpm@10.5.1 install --frozen-lockfile)
  ```

- **"PostgreSQL is not ready"**: start it with `docker compose up -d postgres`.
  If it still fails with an authentication error, the database volume was
  created with a different `POSTGRES_PASSWORD`; put the old password back in
  `.env`, or reset the database with `docker compose down -v` (this deletes
  all studies).
- **"AUTH_MODE=api_key requires API_KEY_PEPPER"**: set a 16+ character
  `API_KEY_PEPPER` in `.env`. Changing the pepper later invalidates every
  issued key; issue a new one with step 6.
- **The UI rejects the API key**: issue a new key (step 6); a lost key cannot
  be recovered.
- **A domain shows "not available in this build"**: that tier or source is not
  built for the species; fetch and build the tier from step 5.
- **Agent lanes fail with authentication errors**: check the provider key in
  `.env` and that `MODEL` names that provider (`anthropic:...` or
  `openai:...`), then restart `start-dev.sh`.
