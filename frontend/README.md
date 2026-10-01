# AgriHub Agent Platform UI

Next.js research-tool interface for the AgriHub Agent Platform: a studies
dashboard at `/`, the study form at `/studies/new`, and the live multi-agent
run view at `/studies/[threadId]`. The original chat lives at `/chat`.

## Configuration

Copy `.env.example` to `.env.local`:

```dotenv
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
NEXT_PUBLIC_ASSISTANT_ID=agrihub_study
NEXT_PUBLIC_CHAT_ASSISTANT_ID=agrihub
```

`NEXT_PUBLIC_ASSISTANT_ID` is the study graph; an older `.env.local` that still
sets it to `agrihub` points the studies UI at the chat graph and must be
updated. The API URL and assistant ids come from the environment. They do not replace
the platform API key. The sign-in form always asks for that key and sends it
as `X-Api-Key`. By default the key is kept in session storage for the tab.
Choosing “Remember this key” stores it in localStorage; any script that runs
on this origin can read that copy.

## Development

```bash
pnpm install
pnpm dev
```

Open <http://127.0.0.1:3000>.

## Checks

```bash
pnpm lint
pnpm format:check
pnpm exec tsc --noEmit
pnpm build
pnpm vitest run
```

`src/lib/__fixtures__/poster-run.ndjson` is the golden event stream of the
poster study; regenerate it with `uv run python scripts/record_study_events.py`
from `agent-platform/` when the backend event contract changes.

The UI supports streaming messages, thread history, cancellation,
regeneration/editing, multimodal inputs, artifacts, and human-in-the-loop
interrupts. The backend implements these capabilities incrementally through
the platform migration plans.

## License and attribution

This UI is derived from LangChain's Agent Chat UI. The original MIT license is
preserved in `LICENSE`.
