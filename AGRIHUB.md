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

Open <http://127.0.0.1:3000>. The UI is preconfigured to use the `agrihub`
graph at <http://127.0.0.1:2024>.

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

Local development intentionally runs without authentication. Before exposing
the services publicly, restore an authentication provider in `langgraph.json`,
enforce per-user thread access, and isolate command or scientific-job execution
in separate containers.
