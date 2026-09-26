# AgriHub Agent Platform UI

Next.js chat interface for the AgriHub Agent Platform.

## Configuration

Copy `.env.example` to `.env.local`:

```dotenv
NEXT_PUBLIC_API_URL=http://127.0.0.1:8000
NEXT_PUBLIC_ASSISTANT_ID=agrihub
```

The API URL and assistant id come from the environment. They do not replace
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
pnpm build
```

The UI supports streaming messages, thread history, cancellation,
regeneration/editing, multimodal inputs, artifacts, and human-in-the-loop
interrupts. The backend implements these capabilities incrementally through
the platform migration plans.

## License and attribution

This UI is derived from LangChain's Agent Chat UI. The original MIT license is
preserved in `LICENSE`.
