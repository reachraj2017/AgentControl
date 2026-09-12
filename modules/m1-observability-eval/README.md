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
populate with no code in the target process.

A background loop groups every gateway call by trace/conversation/run
identity; a group is only synthesised into eval targets if no in-process
trace already exists for the same identity — otherwise the in-process trace
is authoritative and the gateway row is merged as metadata only (never
double-scored). For a call carrying a real propagated `trace_id`, this loop
waits (up to a bounded grace period) for an in-process trace to appear
before concluding none is coming and synthesising from the gateway row
alone — a real in-process tracer's own debounce and LLM-judge cascade can
take tens of seconds, and synthesising too early would create a premature,
partial evaluation alongside the eventual complete one. Purely gateway-only
integrations (no propagated `trace_id` at all) are unaffected by this wait
and stay instant.

Evaluation work itself (the LLM-judge cascade) runs on a worker thread
rather than blocking the service's event loop, so it doesn't stall new
trace ingestion or other background processing while a long-running
evaluation is in progress.

The eval trigger debounces: a new completed span for a trace cancels and
reschedules any pending evaluation for that trace, so a multi-agent
conversation whose spans arrive across several OTLP batches is evaluated
exactly once, shortly after the *last* completion — not once per completion.

When a trace contains multiple task-shaped spans for the same logical
invocation (for example, a framework's own native span, like Google ADK's
`invoke_agent <agent>`, nested inside a manually-created span for the same
call), only the outermost span counts as the invocation — a nested,
framework-native representation of the same call is not treated as a second
one. The same principle applies one layer down to LLM-call spans: when a
single real LLM call is represented by a chain of nested spans at different
instrumentation layers, only one is counted toward token/cost totals, never
the whole chain.

For conversation and run-id attribution specifically, when a batch contains
both an ACP-native task span and a framework-native one for the same trace,
the native span is preferred as the source of identity — only it reliably
carries the plain `conversation.id`/`run.id` keys this system reads; a
framework-native span may use a differently namespaced attribute for
conversation identity and typically has no run-id equivalent at all.

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
