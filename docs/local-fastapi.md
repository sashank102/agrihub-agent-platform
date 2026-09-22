# Local FastAPI chat server

The FastAPI server is the single-process chat protocol API. With
`AUTH_MODE=disabled` it is an explicit local-development bridge and every
request acts as `DEVELOPMENT_USER_ID`. That mode cannot start when
`ENVIRONMENT=production`. Production requires `AUTH_MODE=api_key` and an
`API_KEY_PEPPER` of at least 16 characters. There is no default pepper.
The server never accepts owner identity from request metadata. The frontend
stays on `langgraph dev` until Plan 07.

## Start

Apply the platform migrations, then run the API with one process:

```bash
docker compose up -d postgres
./.tools/bin/uv run alembic upgrade head
./start-api.sh
```

The API listens on <http://127.0.0.1:8000> by default. The existing
`./start-dev.sh` and `langgraph dev` path remains unchanged on port `2024`.

Exactly one Uvicorn worker is required. `start-api.sh` starts Uvicorn with
`workers=1` and refuses `WEB_CONCURRENCY` greater than 1. Lifespan also holds
PostgreSQL advisory lock classid `1095914057` and objid `1214579201` on a
dedicated connection, so `uvicorn agent_platform.main:app --workers 2` cannot
start a second RunManager. Run tasks and SSE subscriber queues live in that
process. PostgreSQL stores the ordered event log, so a client can disconnect
and reconnect to this same process. A second worker or a new process after a
crash cannot see those queues. Startup marks leftover `pending` and `running`
rows `interrupted` unless a terminal write is still queued for reconciliation,
and does not resume model or tool calls. Retry or resume is an explicit new
run from the last checkpoint.

## API keys

Create users and keys with the admin CLI. The plaintext key is printed once
on stdout. Do not pass it as an argument.

```bash
python -m agent_platform create-user --display-name "Service"
python -m agent_platform issue-key --user-id "<user-id>" --label "local"
python -m agent_platform list-keys --user-id "<user-id>"
python -m agent_platform revoke-key --key-id "<key-id>"
python -m agent_platform rotate-key --key-id "<key-id>"
```

Keys look like `aghub_` plus a short lookup prefix plus at least 256 bits of
secret. Only the prefix and an HMAC-SHA256 of the secret are stored. Send the
key as `X-Api-Key` or `Authorization: Bearer`. Missing, invalid, revoked,
expired, conflicting, and inactive-user credentials all return 401 with
`invalid authentication credentials`.

## Supported protocol subset

- `GET /health`
- `GET /ready`
- `GET /info`
- `POST /threads`
- `POST /threads/search`
- `GET /threads/{thread_id}`
- `GET /threads/{thread_id}/state`
- `POST /threads/{thread_id}/history`
- `POST /threads/{thread_id}/runs/stream`
- `GET /threads/{thread_id}/runs/{run_id}/stream`
- `POST /threads/{thread_id}/runs/{run_id}/cancel`

Thread responses use `thread_id`, `created_at`, `updated_at`,
`state_updated_at`, `metadata`, `status`, `values`, and `interrupts`. Agent
Protocol `status` is `idle`, `busy`, `interrupted`, or `error`, derived from
run rows and checkpoint interrupts. Platform lifecycle values `active`,
`archived`, and `deleted` stay separate; deleted threads are omitted from
search and return 404 from get, state, history, and runs.

State and history responses use `values`, `next`, `checkpoint`, `metadata`,
`created_at`, `parent_checkpoint`, and `tasks`. History accepts `limit`
(default 10), `before`, `metadata`, and `checkpoint`.

Streams persist every externally visible event in `platform.run_events`
before publishing it. The SSE `id` is that per-run sequence. Reconnect with
`Last-Event-ID` or `last_event_id` replays later events and, while the run is
still active in this process, continues from the local subscriber queue.
`Content-Location`, `X-Run-ID`, and `X-Thread-ID` identify the run.

Cancellation sets `cancellation_requested`, cancels the in-process task when
it exists, and commits `cancelled`. Repeating the call is idempotent.
`wait=0` still returns only after that terminal row is committed.
`action=rollback` is rejected because checkpoints are not rewritten.

Run submission honors `command.resume`, `command.goto`, `command.update`, and
a checkpoint on the same thread. Resume is rejected when the checkpoint is
not interrupted. A checkpoint id from another thread is rejected.

## Request fields

| Field | Behavior |
| --- | --- |
| `input`, `assistant_id`, `config`, `context` | Executed. `config.configurable.thread_id` is forced to the path thread. |
| `metadata` | Stored with the run configuration. |
| `stream_mode` | Only `values`. Anything else is 422. |
| `stream_subgraphs` | Passed through to LangGraph. |
| `stream_resumable` | Accepted and ignored. Events are always stored. |
| `command.resume`, `command.goto`, `command.update` | Converted to a LangGraph `Command`. |
| `command.graph` | 422. |
| `checkpoint` | Forks the same thread. Cross-thread ids are rejected. |
| `interrupt_before`, `interrupt_after` | Passed through. `*` or node names. |
| `durability` | `sync` (default), `async`, or `exit`. |
| `checkpoint_during` | Passed through when `durability` is omitted. |
| `multitask_strategy` | Only `reject` (also the default). Other strategies are 422. |
| `on_disconnect` | `continue` (default) or `cancel`. |
| `on_completion` | Only `keep`. |
| `if_not_exists` | Only `reject`. |
| `webhook`, `after_seconds`, `feedback_keys` | 422. |

`/info` advertises this subset and lists the unsupported behaviors. It does
not claim webhooks, background workers, or multi-node execution.

## Configuration

The server reads `API_HOST`, `API_PORT`, `API_ALLOWED_ORIGINS`,
`API_MAX_REQUEST_BODY_BYTES`, `API_MAX_CONCURRENT_RUNS`,
`API_STREAM_SUBSCRIBER_QUEUE_SIZE`, `DEVELOPMENT_USER_ID`,
`DEVELOPMENT_USER_EMAIL`, `DEVELOPMENT_AGENT_ID`, and
`DEVELOPMENT_GRAPH_ID`. Local CORS defaults are `http://127.0.0.1:3000` and
`http://localhost:3000`. List-valued origins use Pydantic's JSON environment
syntax.

Request bodies are limited while chunks arrive. A declared `Content-Length`
above the limit is rejected before the body is read. Chunked bodies are
counted incrementally and rejected with 413 as soon as the limit is crossed;
the remainder is not read. Invalid `Content-Length` values return 400.
