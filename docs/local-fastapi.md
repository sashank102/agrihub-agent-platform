# Local FastAPI chat server

The FastAPI server is an unauthenticated local-development bridge for the
current LangGraph SDK chat flow. It always acts as the explicitly configured
development user and never accepts owner identity from request metadata. Do
not bind it to a public interface or present it as production-safe.
`ENVIRONMENT=production` prevents this server from starting; Plan 06 adds
authentication and disabled/deleted-user enforcement.

## Start

Apply the platform migrations, then run the API with one process:

```bash
docker compose up -d postgres
./.tools/bin/uv run alembic upgrade head
./start-api.sh
```

The API listens on <http://127.0.0.1:8000> by default. The existing
`./start-dev.sh` and `langgraph dev` path remains unchanged on port `2024`.
The frontend is not switched to FastAPI until Plan 07.

One Uvicorn worker is required because run admission uses an in-process
`asyncio.Semaphore` and graph execution happens in-process. Multiple workers
would enforce separate limits and do not provide the durable queue/replay
semantics deferred to Plan 05.

## Supported protocol subset

- `GET /health`
- `GET /ready`
- `GET /info`
- `POST /threads`
- `POST /threads/search`
- `GET /threads/{thread_id}`
- `GET /threads/{thread_id}/state`
- `POST /threads/{thread_id}/runs/stream`

Thread responses use `thread_id`, `created_at`, `updated_at`,
`state_updated_at`, `metadata`, `status`, `values`, and `interrupts`. State
responses use `values`, `next`, `checkpoint`, `metadata`, `created_at`,
`parent_checkpoint`, and `tasks`. Streams emit an initial `metadata` SSE event
containing `run_id` and `thread_id`, followed by `values` events. The
`Content-Location`, `X-Run-ID`, and `X-Thread-ID` headers also identify the
run.

Current SDK request fields are accepted so ordinary clients can send them, but
only input, assistant/graph selection, config, metadata tags, and `values`
streaming are acted upon. Cancellation, reconnect/replay, resumable streams,
branching/checkpoint selection, regenerate, HITL commands/resume, background
workers, durable run events, and advanced stream modes are deferred to Plan
05.

## Configuration

The server reads `API_HOST`, `API_PORT`, `API_ALLOWED_ORIGINS`,
`API_MAX_REQUEST_BODY_BYTES`, `API_MAX_CONCURRENT_RUNS`,
`DEVELOPMENT_USER_ID`, `DEVELOPMENT_USER_EMAIL`, `DEVELOPMENT_AGENT_ID`, and
`DEVELOPMENT_GRAPH_ID`. List-valued origins use Pydantic's JSON environment
syntax, for example `["http://127.0.0.1:3000"]`.
