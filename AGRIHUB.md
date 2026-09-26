# AgriHub agent platform

This folder combines:

- `open_deep_research` as the LangGraph multi-agent backend
- `frontend/` as the connected Agent Chat UI
- A provider-agnostic model layer for clarification, planning, parallel
  research, compression, and final report generation

## Run it

Add your key to `.env`:

```dotenv
ANTHROPIC_API_KEY=sk-ant-...
```

For free-tier testing with Groq and keyless DuckDuckGo search:

```bash
cp .env.groq.example .env
# Add GROQ_API_KEY to .env
./start-dev.sh
```

The shared `MODEL` setting accepts provider-prefixed LangChain model names such
as `anthropic:...`, `groq:...`, `openai:...`, and `google_genai:...`. The model
must support tool calling and structured output. Provider keys use the
conventional `<PROVIDER>_API_KEY` environment variable.

Then start both services:

```bash
./start-dev.sh
```

Open <http://127.0.0.1:3000>. The UI talks to the FastAPI server at
<http://127.0.0.1:8000> and asks for a platform API key.

If dependencies need to be recreated:

```bash
./setup.sh
```

## Current workflow

1. Ask for missing species, trait, assembly, SNP, or study inputs.
2. Convert the conversation into a structured research brief.
3. Have a supervisor delegate independent research tasks concurrently.
4. Run the configured web search provider and any configured MCP tools.
5. Compress evidence from each research agent.
6. Generate a citation-backed report.

This is a working research-agent shell. It does not yet calculate SNPs or map
coordinates itself. Those operations should be deterministic tools rather than
prompt logic.

## Add domain tools

The backend already accepts MCP tools through `mcp_config`. Add crop-genomics
services as MCP servers or Python tools for:

- model discovery and execution
- assembly and SNP normalization
- locus construction
- SNP-to-gene mapping
- SoyBase, Gramene, MaizeGDB, and Ensembl Plants
- QTL, expression, orthology, pathway, and publication evidence

The main extension points are:

- `src/open_deep_research/configuration.py` — models, concurrency, MCP settings
- `src/open_deep_research/prompts.py` — coordinator and evidence-agent policy
- `src/open_deep_research/utils.py` — tool discovery and execution
- `src/open_deep_research/deep_researcher.py` — workflow nodes and routing
- `frontend/src/components/thread/` — product UI

## Production warning

Local development can run with `AUTH_MODE=disabled`. Production requires
platform API keys. Thread and run access is limited to the key's user.
Deployment packaging is owned by a later plan.
