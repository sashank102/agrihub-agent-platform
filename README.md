# AgriHub Agent Platform

A self-hosted, provider-agnostic multi-agent research platform built on
LangGraph, PostgreSQL, and a Next.js chat interface.

## Current architecture

- `src/open_deep_research/` — active coordinator/researcher LangGraph workflow
- `src/agent_platform/` — PostgreSQL persistence, platform metadata models, and repositories
- `alembic/` — migrations for the platform-owned PostgreSQL schema
- `frontend/` — Next.js chat client using the LangGraph SDK
- `compose.yaml` — PostgreSQL, one FastAPI process, and the Next.js UI
- `Dockerfile` and `frontend/Dockerfile` — production images
- `.github/workflows/ci.yml` — lint, tests, browser checks, and image build

`./start-dev.sh` starts PostgreSQL-backed FastAPI on port `8000` and the
Next.js UI on port `3000`. It defaults to `AUTH_MODE=api_key` and requires
`API_KEY_PEPPER` plus a key from the admin CLI. `AUTH_MODE=disabled` is an
explicit development bridge; the UI labels it as such, and production rejects
it. `./start-prod.sh` builds the Compose stack. There is no Redis and only
one API worker. The in-memory LangGraph development server is not part of
normal development.

## Local setup

```bash
./setup.sh
cp .env.example .env
# Add API_KEY_PEPPER (16+ characters) and the selected model-provider key.
docker compose up -d postgres
./.tools/bin/uv run alembic upgrade head
./.tools/bin/uv run python -m agent_platform create-user --display-name "Local"
./.tools/bin/uv run python -m agent_platform issue-key --user-id "<user-id>" --label local
./start-dev.sh
```

Open <http://127.0.0.1:3000> and paste the printed API key.

Production on one machine:

```bash
# Export POSTGRES_PASSWORD, API_KEY_PEPPER, MODEL, and the selected provider
# API key first. Do not commit them.
./start-prod.sh
```

Production Compose has no fallback database password or API-key pepper. The API
container receives supported provider credentials only at runtime; they are not
baked into either image. Run retention as a one-shot maintenance container:

```bash
docker compose --profile maintenance run --rm retention
```

Operations: [backup and restore](docs/operations/backup-restore.md),
[migrations](docs/operations/migrations.md),
[API keys](docs/operations/api-keys.md),
[reconciliation](docs/operations/reconciliation.md).

For free-tier Groq testing:

```bash
cp .env.groq.example .env
# Add GROQ_API_KEY to .env
./start-dev.sh
```

## Species data bundles

Study tools read an offline DuckDB bundle per species under
`AGRIHUB_DATA_DIR` (default `var/data`). Each tier includes the ones before
it, and `build` writes the bundle for the tier it is given:

```bash
# core: genes, annotation, id maps, markers, QTL, GWAS, curated genes, orthology, ontologies (~2.3 GB raw)
./.tools/bin/uv run agrihub-data fetch --species soybean --tier core
# extended: expression atlases, STRING and ATTED-II networks, PlantTFDB/PlantRegMap, PMN and Plant Reactome (~6 GB more raw)
./.tools/bin/uv run agrihub-data fetch --species soybean --tier extended
# heavy: VEP cache, SoySNP50K LD panel, GmHapMap, HXNY homeologs (~0.3 GB more raw)
./.tools/bin/uv run agrihub-data fetch --species soybean --tier heavy
scripts/setup_heavy_tools.sh   # PLINK2 binary and the ensembl-vep Docker image
./.tools/bin/uv run agrihub-data build --species soybean --tier heavy
./.tools/bin/uv run agrihub-data verify --species soybean
```

Rice (IRGSP-1.0), maize (Zm-B73 NAM-5.0) and sorghum (BTx623 NCBIv3) use the
same commands and the same tools; only their registry files
(`src/agrihub_data/registry/<species>.yaml`) and source parsers differ.
`budget` prints the download size per tier from the registry before fetching:

```bash
./.tools/bin/uv run agrihub-data budget --species rice
# core: RAP-DB models, annotation and RAP-MSU map, RAP-DB curated genes, Oryzabase, funRiceGenes,
#       Gramene QTL, GWAS Atlas, Ensembl Compara, PLAZA monocots, TAIR, GO/TO/PO/CO_320 (~322 MB raw)
# extended: PMN OryzaCyc and Plant Reactome (~26 MB more)
./.tools/bin/uv run agrihub-data fetch --species rice --tier extended
./.tools/bin/uv run agrihub-data build --species rice --tier extended
./.tools/bin/uv run agrihub-data verify --species rice
```

- Maize (~290 MB core): MaizeGDB v5 models, annotation, v4->v5 gene xref and
  chain file (GWAS Atlas rows are on v4 and are lifted to v5 at build time),
  classical genes, Wallace 2014 NAM GWAS on v5. Fetched from
  `download.maizegdb.org`; the main MaizeGDB site blocks scripts.
- Sorghum (~220 MB core): SorghumBase NCBIv3 models, GWAS Atlas (the Sbi1.4
  rows are dropped), the cloned Ma/Dw genes of Grant et al. 2023 shipped in
  `src/agrihub_data/curated/`, PLAZA, Compara.
- Two sorghum sources are manual downloads that `fetch` never scripts. It
  records them once they are saved in place, and `prune-raw` never deletes
  them:
  - Phytozome `Sbicolor_454_v3.1.1.annotation_info.txt` needs a JGI or ORCID
    login: save it as `var/data/sorghum/raw/phytozome_annotation_info/Sbicolor_454_v3.1.1.annotation_info.txt`.
  - The Sorghum QTL Atlas has no scriptable export: build `SorghumQtlAtlas.db`
    with `github.com/jlboat/query_qtl_atlas` from the Atlas Excel exports and
    save it as `var/data/sorghum/raw/sorghum_qtl_atlas/SorghumQtlAtlas.db`.

  Then run `fetch` and `build` again.
- Known gaps are explicit in the registry (`status: planned` sources with
  `provides`) and reported per domain by `availability`: maize QTL (no
  positioned maize QTL table), sorghum expression (MOROKOSHI is down), rice
  and maize expression (sample sheets not curated yet), RiceNet and Q-TARO.
- Licences are recorded per source; GWAS Atlas, PlantTFDB and PlantRegMap are
  flagged academic-only and the report's Sources list says so.

Tools never read the raw downloads, so they can be deleted once the bundle
verifies. `prune-raw` refuses to delete anything otherwise, and keeps every
file's URL and sha256 in `manifest.json`; `fetch` restores them and fails if
upstream bytes changed since pruning. `status` shows disk use per tier:

```bash
./.tools/bin/uv run agrihub-data prune-raw --species soybean --keep core --dry-run
./.tools/bin/uv run agrihub-data prune-raw --species soybean   # all raw files
./.tools/bin/uv run agrihub-data status --species soybean
```

After `prune-raw --keep core`, rebuilding the extended or heavy tier of any
species needs `fetch --tier extended` (or `heavy`) first: `build` refuses to
run while pruned files are missing and says which sources to re-fetch.

Domains whose data or binaries are missing are reported to the agents and in
the report as "not available in this build"; `/registry/species` lists them.

## PostgreSQL

```bash
docker compose up -d postgres
docker compose ps postgres
```

See:

- [PostgreSQL graph persistence](docs/postgres-persistence.md)
- [Platform metadata schema](docs/platform-schema.md)
- [Local FastAPI chat server](docs/local-fastapi.md)
- [Project-specific setup and extension points](AGRIHUB.md)

## Verification

```bash
./.tools/bin/uv run pytest -m "not postgres"
cd frontend && pnpm build
```

PostgreSQL integration tests require `TEST_DATABASE_URI`; the persistence docs
contain the complete command.

## License and attribution

This project contains code derived from LangChain's Open Deep Research and
Agent Chat UI projects. Their MIT license notices remain in this repository.
