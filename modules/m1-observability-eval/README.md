# M1 — Observability & Eval

M1 is the foundation of the AI Control Plane. It ingests OpenTelemetry spans from any agent framework, stores them in ClickHouse, and runs a suite of 68 evaluation metrics against every LLM call automatically.

## What it does

- Receives OTLP traces via the collector (gRPC :4317, HTTP :4318) and writes them to ClickHouse
- Runs the eval-runner service, which evaluates every completed span for correctness, cost, latency, tool usage, and safety
- Supports LLM-as-judge scoring (Anthropic, OpenAI, Ollama) and statistical heuristics
- Tracks eval benchmarks over time and flags regressions
- Exposes results in the portal at http://localhost:8888

## Portal pages

| Page | What you can do |
|------|----------------|
| Eval Testing | Manage benchmark test cases and runs, execute benchmarks, view scores, regression analysis |
| Eval Measurements | Time-series metric trends, regression detection across runs |

## Eval Testing — how it works

The Eval Testing page connects benchmark test cases to agent runs. When executed, each benchmark task is sent to an **Agent Endpoint** (your running agent), which processes it through the full pipeline (including the gateway). OTel spans flow automatically to the eval-runner and scores appear within seconds.

```
Eval Testing page
  → POST /chat to Agent Endpoint (e.g. opt-demo/server.py on :8090)
      → runner.py → ADK agents → Gateway (M3) → LLM
          → OTel spans → eval-runner → 68 metrics scored
              → Scores tab / Regression tab
```

### Tabs

**Runs tab**
- Create runs with a name, suite, agent version, run type, and agent endpoint URL
- Attach benchmark test cases to a run
- Execute a run — progress shown inline, one benchmark at a time
- Mark any run as the ⭐ baseline for regression comparison
- Group runs with a **Run Group** tag to enable suite-level execution and cross-run comparison
- Trigger offline re-evaluation to re-score all traces in a run (e.g. after changing evaluator config)
- Execution history with status, duration, and direct Jaeger trace links

**Benchmarks & Review tab**
- Create and manage benchmark test cases: task input, expected output, rubric (JSON), suite, difficulty, dataset version, tags
- Human review queue — review auto-scored evals, override scores, add notes

**Scores tab**
- Per-metric average score bar chart for any run
- Score distribution histogram
- Score trend lines over the last 7 days
- Individual score table with eval type, evaluator, and LLM judge reasoning

**Regression tab**
- Pick any two runs; compare per-metric scores side-by-side
- Delta table: Regressed 🔴 / Improved 🟢 / Unchanged ⚪ per metric
- Radar chart overlay of current vs baseline

## Setting up benchmark runs with opt-demo

### 1. Start the benchmark server

```bash
cd opt-demo
python server.py        # listens on http://localhost:8090
```

### 2. Create benchmark test cases (Benchmarks & Review tab)

Add task inputs that match your agents' capabilities:

```
Search for latest breakthroughs in quantum computing
Find info on climate change and summarize in 30 words
Search quantum computing, summarize in 20 words, translate to Hindi
Translate 'Good morning, how are you?' to Japanese
```

Optionally add an expected output or JSON rubric for custom LLM-judge scoring.

### 3. Create a run (Runs tab)

Set **Agent Endpoint** to:
```
http://host.docker.internal:8090/chat
```
> Use `host.docker.internal` (not `localhost`) — the portal runs inside Docker.

Select the benchmark test cases to include, then click **Create Run**.

### 4. Execute and view scores

Click **▶** on the run. Scores appear in the **Scores** tab and **Run Drill-Down** within a few seconds of each execution.

For model comparison across routing variants or A/B test configurations, tag runs with a **Run Group** and use **Compare Scores** to see a pivot table across all runs.

## Run standalone

```bash
cp .env.example .env
# Add ANTHROPIC_API_KEY to .env
make up-m1
```

## Instrumenting external agents

Use the `acp-tracing` SDK to emit OTLP spans from any external multi-agent system into M1:

```bash
# Not published to PyPI — install by path (see docs/instrumentation-guide.md "Prerequisites")
pip install "/path/to/this/repo/sdk/packages/acp-tracing[otel]"
```

See [`sdk/packages/acp-tracing/README.md`](../../sdk/packages/acp-tracing/README.md) for full usage.

## Gateway-only integration (no ACP SDK required)

M1 no longer requires the in-process `acp-tracing` SDK to produce evals. A
background `GatewayIngestPipeline` (`pipeline/gateway_ingest_pipeline.py`)
polls `gateway_call_log` (+ `gateway_structural_events` for handoff/tool-span
signals from M3) every 5s, groups rows into traces (by `trace_id` →
`conversation_id` → `run_id` → singleton call), and — if no in-process trace
already exists for that key — synthesises a flat `agent.task` + `llm_call`
target set and runs it through the exact same `EvalPipeline.run_from_targets()`
code path the in-process span-tree path uses. Point any framework's
`base_url` at the gateway (M3) and Eval Measurements / Prompt Analysis
populate with no code in the target process. See
`design/v2-gateway-capture-m1-ingest.md` and
`design/checkpoint-handoff-ingest.md` for the full design.

> **Validated against a live stack** — see `design/v4-implementation-status.md`
> for the full write-up, including a critical bug found and fixed during that
> pass: `get_pending_gateway_calls()` checked `st.state IS NULL` after a
> `LEFT JOIN`, but ClickHouse fills unmatched rows with the column's type
> default (empty string) rather than SQL `NULL` — so the condition never
> matched and this entire pipeline silently processed zero rows, forever,
> with no error. Fixed to check `st.state = ''`. Confirmed end-to-end after
> the fix: a zero-SDK client hitting the gateway directly now produces full
> (37-metric) eval coverage, and `opt-demo`'s in-process trace correctly
> triggers MERGE mode (no duplicate scoring) rather than SYNTHESISE.
>
> A second bug was found from a user-reported symptom (Prompt Analysis
> showing each agent turn twice — once with real content and no token cost,
> once as agent `"unknown"` with cost but no content): the additive
> standards-recognition below was adding ADK's own native `invoke_agent
> <agent>` span as a *second* task span even though it's nested inside
> opt-demo's own `agent.task` span for the same invocation, splitting one
> invocation's token attribution across two rows. Fixed in
> `ingestion/trace_assembler.py` — a dialect-recognized task span is now
> skipped if any ancestor is already a recognized task span (native or
> dialect), so only the outermost span per branch counts.
>
> A third bug, same root cause one layer down: Prompt Analysis token counts
> ran ~3x what `gateway_call_log` showed for the same call. A single real
> LLM call is represented by a nested chain (ADK's `call_llm` wrapper →
> `generate_content <model>` → `opentelemetry-instrumentation-openai`'s
> `openai.chat` child) — the native code already picked exactly one of these
> to avoid double-counting, but the additive dialect probes didn't know
> about that exclusion and silently re-added the other two. Fixed the same
> way: a dialect-recognized LLM span is skipped if it's an ancestor *or*
> descendant of an already-recognized LLM span.
>
> A fourth, unrelated bug turned up on a genuinely multi-hop query (search
> then translate — 3 agent hops): the eval trigger used a fixed 30s
> cooldown from the *first* completed agent-task span seen for a trace, not
> a true debounce. Multi-agent conversations export their spans
> incrementally (one OTLP batch per sub-agent), so anything slower than
> 30s end-to-end let a later sub-agent's completion fire a second,
> independent evaluation on top of the first — a 3-agent trace produced 5
> `prompt_evals` rows instead of 3. Fixed in `main.py`: replaced the
> timestamp cooldown with a real debounce (`_pending_eval`) — every new
> completion cancels and reschedules the pending evaluation, so exactly one
> pass runs, 10s after the *last* completion rather than 10s after the
> first.
>
> That fix was necessary but not sufficient — a longer (4-hop) query still
> showed duplicates, and the user correctly rejected "it's about hop count"
> as the explanation. The real, more fundamental cause: opt-demo's calls
> are captured *twice* — once in-process, once by the gateway itself (it
> also routes through M3). `GatewayIngestPipeline` polls every 5s and, for
> a real propagated `trace_id`, assumed "gateway-only" the instant it
> didn't yet see in-process spans — but the in-process pipeline routinely
> takes 30-90s+ (its own debounce plus a full LLM-judge cascade) to finish,
> so the 5s poller kept winning the race and writing premature partial
> passes under its own run_id, entirely bypassing the debounce above. Not a
> function of hop count at all — a single-agent call races the same way,
> just with better odds. Fixed in `gateway_ingest_pipeline.py`: a real
> propagated `trace_id` is itself evidence an in-process tracer is active,
> so instead of synthesising the moment in-process spans aren't found yet,
> those rows are left pending and re-checked on the next poll for up to
> `_INPROCESS_GRACE_SECONDS` (120s) before finally assuming gateway-only.
> Purely-synthetic gateway-only groups (no real trace_id) are untouched —
> the zero-SDK integration path stays instant.
>
> Two more, found while diagnosing "why do I see no conversation scores":
> (1) `pipeline.run()` — the actual LLM-judge evaluation cascade — was called
> as a bare synchronous call inside an `async def`, with no
> `asyncio.to_thread()`. Since asyncio's event loop is single-threaded, that
> blocked the *entire process* (new trace ingestion, health checks, every
> other background loop) for the run's full 15-90+ second duration —
> confirmed via the OTel Collector's own logs timing out trying to reach
> eval-runner. Fixed by wrapping all three such call sites in
> `asyncio.to_thread()`, matching the pattern `_shadow_eval_loop` and
> `_gateway_ingest_loop` already used correctly. (2) Even after that fix,
> conversation tracking stayed empty: when opt-demo's dual instrumentation
> puts *both* the ACP-native `agent.task` span and ADK's own native
> `invoke_agent <agent>` span in one batch, the code picked whichever
> arrived first with no preference — and `invoke_agent <agent>` carries the
> namespaced `gen_ai.conversation.id`, not the bare `conversation.id` key
> read here, and no run-id attribute at all, so picking it silently broke
> conversation tracking *and* risked misattributing the eval to the
> generic "default" run. Fixed by explicitly preferring the native span per
> trace_id when one exists in the batch.

## Standards-based span recognition

M1's trigger (`pipeline/trigger.py`) and trace assembler
(`ingestion/trace_assembler.py`) no longer only recognize ACP's own literal
`agent.task` span name. `ingestion/semconv_mapping.py` is a normalization
layer that also recognizes:

- **OTel GenAI Semantic Conventions** — `invoke_agent` operation, `chat
  <model>` / `generate_content <model>` spans, `gen_ai.*` attributes.
- **OpenInference** (Arize) — `openinference.span.kind` ∈
  {AGENT, CHAIN, LLM, TOOL}.
- **OpenLLMetry** (Traceloop) — `traceloop.span.kind` ∈
  {workflow, task, agent, tool} plus `gen_ai.*` attributes.

This means a raw trace from an unmodified external framework — via a
community-maintained OpenInference or OpenLLMetry auto-instrumentor, or a
framework's own native OTel export — triggers evaluation and extracts
task/tool/handoff/LLM targets correctly, without requiring ACP's own tracer
at all. Run `python3 ingestion/semconv_mapping.py` for the dialect
conformance self-test. Extra proprietary span names can be added via
`config/evaluator_config.yaml`'s `ingestion.task_span_names` without a code
change.

## M1 sub-agent (eval-agent)

In addition to the eval-runner service, M1 includes a domain-specific LLM agent that the EvalGov coordinator delegates to for all eval-related questions.

**What it handles:** eval runs, benchmarks, eval scores, OTel traces, agent performance, token costs, error rates, safety events, thresholds, agent budgets, version pins, lifecycle changes, model registry, compliance scorecard, risk register, prompt detail, search prompts.

The sub-agent is stateless — it receives a query + optional history, runs its own LLM tool loop (~23 tools), and returns a plain-text response. It starts automatically as part of `make up` and requires `eval-runner` to be healthy first.

## Key ports

| Service | Port |
|---------|------|
| eval-runner | 8000 |
| eval-agent (M1 sub-agent) | 8001 |
| portal | 8888 |
| otel-collector (gRPC) | 4317 |
| otel-collector (HTTP) | 4318 |
| Jaeger UI | 16686 |
| ClickHouse HTTP | 8123 |
