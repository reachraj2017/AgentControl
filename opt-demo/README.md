# Opt-Demo — Google ADK + AI Control Plane

A multi-agent system built with Google ADK and instrumented with the AI Control Plane (ACP) SDKs. An orchestrator routes user queries to three specialist sub-agents: Searcher (DuckDuckGo), Summarizer, and Translator.

## Architecture

```
User → Streamlit UI  ─┐
                       ├→ runner.py → ADK Orchestrator
Eval Testing page  ───┘     ├── Searcher   (DuckDuckGo web_search tool)
(via server.py)             ├── Summarizer (condense to word count)
                            └── Translator (any language + transliteration)
                                      ↓
                            ACP Gateway (M3) → LLM provider
                                      ↓
                            OTel spans → eval-runner (M1) → 68 metrics
```

All LLM calls route through the **ACP Gateway (M3)** via `OPENAI_BASE_URL`. All spans are emitted via the **ACP Observability SDK (M1)** to the OTel Collector. **ACP Governance (M2)** checks policies before each agent invocation.

## Prerequisites

1. AI Control Plane running — run `make up` in the parent `v4/` directory.
2. Create a virtual gateway key (required whenever `GATEWAY_AUTH_ENABLED=true`, the default in `.env.example`) and set both `OPENAI_API_KEY` and `GATEWAY_API_KEY` in `opt-demo/.env` to it — a stale or placeholder key will fail auth with a 401:
   ```bash
   MASTER_KEY=$(grep '^GATEWAY_MASTER_KEY=' ../.env | cut -d= -f2-)
   curl -s -X POST http://localhost:8080/gateway/keys \
     -H "Authorization: Bearer $MASTER_KEY" -H "Content-Type: application/json" \
     -d '{"description":"opt-demo","agent_role":"*","system_id":"opt-demo"}'
   # → copy the returned "key" (gw-sk-...) into opt-demo/.env as both OPENAI_API_KEY and GATEWAY_API_KEY
   ```
3. Copy and configure the env file:
   ```bash
   cp .env.example .env
   # Edit .env — set OPENAI_API_KEY/GATEWAY_API_KEY (from step 2) at minimum
   ```
4. Install Python dependencies (a dedicated venv is recommended — this pulls `google-adk`, `litellm`, and several OTel packages):
   ```bash
   python3 -m venv venv && source venv/bin/activate
   pip install -r requirements.txt
   ```

## Run — interactive chat UI

```bash
streamlit run chat_ui.py
```

Opens at `http://localhost:8501`.

## Run — benchmark server (for Eval Testing)

The benchmark server exposes the same agent pipeline as an HTTP API so the ACP Eval Testing page can drive automated benchmark runs programmatically.

```bash
python server.py
# → listening on http://localhost:8090
```

Set `SERVER_PORT` to use a different port:
```bash
SERVER_PORT=9000 python server.py
```

### API

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check — returns `{"status": "ok"}` |
| `POST` | `/chat` | Run a benchmark task through the full agent pipeline |
| `POST` | `/reset?user_id=...` | Clear a user's session state |

**POST /chat request:**
```json
{
  "message":  "Search for Roger Federer and summarize in 20 words",
  "user_id":  "benchmark-abc123",
  "run_id":   "eval-run-uuid",
  "source":   "benchmark"
}
```

**POST /chat response:**
```json
{
  "response":  "Roger Federer is a Swiss tennis legend...",
  "trace_id":  "4b17cd329a8331e180612a69a9fd1862"
}
```

The `trace_id` is the OTel trace ID linking all agent spans for this call. The eval-runner picks it up automatically and scores it within a few seconds.

## Setting up benchmark runs in the Eval Testing portal

### Step 1 — Add benchmark test cases (Benchmarks & Review tab)

Create one test case per scenario. Use explicit inline text for translate/summarize tasks:

| Task type | Example task input |
|-----------|-------------------|
| Search | `Search for latest breakthroughs in quantum computing` |
| Search + summarize | `Find info on climate change and summarize in 30 words` |
| Full pipeline | `Search quantum computing, summarize in 20 words, translate to Hindi` |
| Translate (inline) | `Translate 'Good morning, how are you?' to Japanese` |
| Summarize (inline) | `Summarize in 15 words: The Amazon rainforest produces 20% of the world's oxygen` |

Set difficulty, suite, and optionally an expected output or JSON rubric for custom LLM judge scoring.

### Step 2 — Create a run (Runs tab)

| Field | Value |
|-------|-------|
| Run Name | e.g. `gpt-4o-mini-baseline` |
| Suite | `unit` / `integration` / `collaboration` / `production` |
| Agent Version | e.g. `v1.0` |
| Run Type | `benchmark` |
| Run Group | optional — groups runs for suite-level comparison |
| Agent Endpoint | `http://host.docker.internal:8090/chat` |
| Benchmark Test Cases | select the cases you created in Step 1 |

> **Note:** Use `host.docker.internal` (not `localhost`) as the hostname because the portal runs inside Docker and needs to reach the benchmark server on your Mac.

### Step 3 — Execute

Click **▶** on the run row. The portal sends each benchmark task to the server, which runs the full agent pipeline through the gateway. Progress is shown inline.

### Step 4 — View scores

- **Scores tab** — per-metric bar chart and individual score table for the run
- **Run Drill-Down** — traces, prompt/response pairs, and per-trace scores
- **Regression tab** — compare two runs side-by-side with a radar chart; mark any run as the baseline with ⭐

Eval scores appear automatically within a few seconds of each execution — the OTel spans flow from the agent → gateway → eval-runner without any extra wiring.

### Step 5 — Run suites for model comparison

Tag multiple runs with the same **Run Group** (e.g. `model-comparison-sprint-1`). Use **Execute Suite** to run all of them back-to-back, then **Compare Scores** for a pivot table and bar chart across all runs in the group.

## ACP Instrumentation

- **M1 — Observability**: `acp_setup.py` calls `instrument("opt-demo", ...)` once at import time. Every agent invocation and tool call emits `agent.task` and `agent.tool_call` OTel spans, sent to the ACP Collector → ClickHouse + Jaeger.
- **M2 — Governance**: `GovernanceClient.check_policy()` is called before each agent invocation and tool call. A `block` decision returns a `[GOVERNANCE BLOCK]` message instead of running the agent.
- **M3 — Gateway**: `OPENAI_BASE_URL` is set to `http://localhost:8080/v1`, so every LiteLlm/OpenAI call routes transparently through the ACP Gateway. Gateway routing policies, A/B tests, and enforcement all apply to benchmark runs exactly as they do to live chat.

## Validated end-to-end (v4)

This demo was actually run against the v4 stack — not just reviewed — as part of validating the
gateway-ingest rearchitecture (see `design/v4-implementation-status.md` §4.3 for the full writeup).
That pass found and fixed a real, pre-existing bug in `runner.py`'s regex intent classifier: every
translate/summarize regex anchored on end-of-string, but ordinary sentences end in punctuation
(`"...to Japanese."`), so the trailing period silently broke every match and every translate
request fell back to a broken "translate the previous response" default — the model just echoed
its own system prompt back with no error anywhere. Fixed to strip trailing punctuation before
classifying and to tolerate quoted/apostrophe'd inline text. The example queries below are
confirmed working correctly after that fix; if you're on an older checkout without it, translate
requests with trailing punctuation will silently misbehave.

## Example queries

- `Search for latest AI news`
- `Find info on climate change and summarize in 30 words`
- `Search quantum computing, summarize in 20 words, translate to Hindi`
- `Translate 'Good morning, how are you?' to Japanese`
- `Summarize in 15 words: The Amazon rainforest produces 20% of the world's oxygen and is home to 10% of all species.`
- `Find news about SpaceX and give me a 25-word French summary`
- `Who are you and what can you do?`

## Viewing traces

- **ACP Portal**: `http://localhost:8888` → Eval Measurements
- **Jaeger**: `http://localhost:16686` — trace links appear inline in the chat UI after each response
- **EvalGov Agent**: `http://localhost:8888` → EvalGov Agent — ask natural-language questions (e.g. "which agent had the highest latency today?", "show me all benchmark runs from the last hour")
