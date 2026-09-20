# AgriHub Agent Platform UI

Next.js chat interface for the AgriHub Agent Platform.

## Configuration

Copy `.env.example` to `.env.local`:

```dotenv
NEXT_PUBLIC_API_URL=http://127.0.0.1:2024
NEXT_PUBLIC_ASSISTANT_ID=agrihub
```

A platform API key can be entered in the connection form when authentication is
enabled. It is stored in browser local storage and sent as `X-Api-Key` by the
LangGraph SDK client.

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
