# AgenticAI Control Plane Gateway (v4)

A production-grade control plane built around the **Agent Gateway** — the single point through which every LLM call and agent interaction in your multi-agent system flows. Route your agents through it once, and you get observability, evaluation, governance, enforcement, and conversational intelligence automatically, with no changes to your agent logic.

The platform combines a 68-metric eval pipeline, a 13-category governance engine, gateway routing/caching/A-B/shadow/traffic management, and an EvalGov coordinator + sub-agents behind one portal — all fed by the gateway-primary ingestion model described below.

---

## The core idea

Multi-agent systems make hundreds or thousands of LLM calls — across orchestrators, specialists, tools, and sub-agents. Without a control plane, those calls are invisible: you don't know what they cost, whether they're correct, whether agents are behaving within policy, or when something breaks.

The AgenticAI Control Plane solves this by placing the **Agent Gateway** at the center of all LLM traffic. Every call from every agent routes through the gateway, which then:

- **Logs and traces** the call to the observability layer (M1)
- **Evaluates** it automatically against 68 quality, safety, and performance metrics (M1)
- **Enforces** governance policies — routing rules, rate limits, budget caps, circuit breakers — in real time (M2, M3)
- **Detects anomalies** proactively and surfaces them with AI-generated root cause analysis (M4)
- **Responds to natural language queries** about any agent's behavior, cost, quality, or compliance (M4)

```
Your Multi-Agent System
  │
  │  every LLM call from every agent
  ▼
┌─────────────────────────────────────────────────────┐
│              Agent Gateway (M3)                     │
│  routing · caching · A/B testing · shadow mode      │
│  traffic management · virtual keys · enforcement    │
└───────────────┬─────────────────────────────────────┘
                │ span + call log
    ┌───────────┼───────────────┐
    ▼           ▼               ▼
  M1            M2              M4
  Observability Governance      Intelligence
  & Evaluation  & Enforcement   (EvalGov)
  68 metrics    policy engine   proactive monitor
  ClickHouse    trust scores    LLM root cause analysis
  eval runner   circuit brkrs   conversational interface
                HITL queue      MCP server
```

---

## Architecture: a three-layer capture stack

The control plane's ingestion model rests on one principle: **the gateway is the single ingress boundary for the control plane; standards, not a proprietary contract, define what flows through it.**

**Layer 1 — Gateway wire capture (the floor).** Any framework, any language, zero code beyond a base-URL redirect. Protocol-complete: `/v1/chat/completions`, `/v1/responses` (OpenAI Agents SDK, hosted tools), `/v1/messages` (Anthropic/Claude Agent SDK), Gemini `generateContent` (Google ADK), `/v1/embeddings`. Every call is normalized into one `GatewayCallRecord` shape and written to `gateway_call_log` — durable, immutable, the same table regardless of dialect. M1's `GatewayIngestPipeline` polls this table directly and feeds the real 68-metric pipeline — no bespoke span required. This is also what M2's real-time enforcement, caching, and routing require synchronously in the request path; no passive layer can replace it.

**Layer 2 — Standards-based structural depth.** Rather than relying on a proprietary tracer to reconstruct every framework's internals, M1's trace assembler natively recognizes three industry-standard span dialects via a normalization layer (`ingestion/semconv_mapping.py`): **OTel GenAI Semantic Conventions**, **OpenInference** (Arize), and **OpenLLMetry** (Traceloop). Point any of their community-maintained auto-instrumentors at the OTel collector and richer structure (tool calls, chains, retrieval, agent spans) flows in — maintained upstream by those ecosystems, not by ACP.

**Layer 3 — Explicit checkpoint/handoff/tool-span signals.** Some things never cross the LLM wire at all: a pre-action HITL gate, a sub-agent handoff, a non-LLM tool call. These go through the same gateway front door via three small endpoints (`/v1/checkpoint`, `/v1/handoff`, `/v1/tool-span`) and a thin `acp-signals` SDK client, wired to each framework's *own documented* extension point (OpenAI Agents SDK `RunHooks`, Google ADK callbacks, LangChain `BaseCallbackHandler`, CrewAI step/task callbacks) — never a passive, undocumented patch.

Correlation across all three layers uses one shared primitive: W3C trace context (`trace_id`/`conversation_id`). If a trace already has in-process spans (Layer 2), gateway rows for the same id are merged as metadata only — never double-evaluated.

Structural limits: real-time blocking/routing/caching must stay in the gateway's synchronous path — no passive layer, however standards-based, can gate a call it only observes after the fact. And fully-managed hosted agent runtimes (e.g. server-side tool execution that never leaves a provider's own infrastructure) are structurally out of reach for any capture mechanism that isn't the provider's own log export.

### Known limitations

- **Streaming pass-through is incomplete** for `/v1/responses` and Gemini `:streamGenerateContent` — both currently pass through the underlying chat-completions SSE shape rather than re-dialecting each chunk into the caller's native streaming format.
- **`/v1/embeddings` bypasses routing/governance/cache** — it uses a lean auth-only path, so A/B tests and routing policies don't apply to embedding calls.
- **`/v1/checkpoint` blocks synchronously on HITL** rather than returning `hitl_pending` immediately with separate polling.
- **Gemini `generateContent` routing has not been validated against a real Gemini API key** in this deployment — the request path is implemented, but confirm it yourself before relying on it in production.
- **`gateway_structural_events` and stored message payload retention have no TTL policy yet** — plan for storage growth if running at volume.
- **`backend_used` in the Call Log can display the wrong provider name** for a passthrough-routed call whose model already carries an explicit provider prefix (e.g. shows `"openai"` for an Anthropic call) — cosmetic only; the actual LLM dispatch is correct regardless.

---

## Four integrated modules

### M3 — Agent Gateway (the control plane entry point)

Every LLM call in your multi-agent system routes through the gateway. It operates as a transparent proxy for both OpenAI-compatible and Anthropic-compatible APIs — your agents need no code changes beyond pointing their base URL at the gateway.

**What it does per call:**
- Routes to the right model based on configurable routing policies (cost, latency, quality, fallback chains)
- Checks the exact-match and semantic response cache before hitting the upstream LLM
- Applies pre-execution governance enforcement (key validation, budget checks, model allowlists)
- Logs every call to ClickHouse with full token counts, latency, routing reason, and agent identity
- Emits the call for shadow evaluation or A/B test attribution if applicable

**Traffic management** — group upstream LLM endpoints into pools with 6 load-balancing strategies: `round_robin`, `weighted`, `least_latency`, `performance`, `cost_optimized`, `fallback_chain`. Bind agent roles to pools via traffic policies. Sticky sessions for multi-turn conversations.

**Virtual API keys** — issue `gw-sk-*` keys scoped to specific models, rate limits, and monthly spend budgets. Revoke instantly without touching upstream provider keys.

**A/B testing and shadow mode** — split traffic across model variants and track per-variant quality scores. Run shadow evaluations in parallel without affecting production responses.

---

### M1 — Observability & Evaluation

Every call the gateway logs is automatically picked up by the eval runner and evaluated against **68 metrics** covering:

| Category | Metrics |
|---|---|
| **Quality** | correctness, faithfulness, relevance, coherence, conciseness, hallucination, bias, toxicity |
| **Agent behavior** | task success, tool selection accuracy, tool argument accuracy, tool error rate, step efficiency, handoff fidelity |
| **Safety** | PII detection, prompt injection, instruction following, role adherence |
| **Performance** | latency, token counts, cost, error recovery rate, timeout rate |
| **Multi-turn** | conversation completeness, conversation relevancy, knowledge retention |
| **Trace quality** | context propagation, trace completeness, dead span rate |

Scores are stored in ClickHouse and surfaced in the portal with time-series trend views, regression detection, and per-agent breakdowns. The eval agent (M1 sub-agent) answers natural language questions about scores, benchmarks, regressions, and compliance.

---

### M2 — Governance & Enforcement

Enforces behavioral, safety, and financial policies across all agents — both reactively (on each gateway call) and proactively (via the background watcher).

**13 enforcement categories:** output quality, cost/budget, safety, PII, prompt injection, identity verification, behavioral drift, scope compliance, supply chain integrity, regulatory compliance, HITL approval gates, circuit breakers, and anomaly detection.

**Circuit breakers** — automatically open when an agent exceeds configurable thresholds (error rate, policy violations, rogue behavior). Agents hitting an open CB are blocked at the gateway.

**HITL queue** — agents can be paused and held for human approval before taking irreversible actions. Operators approve or reject directly from the portal.

**Trust scores** — each agent carries a rolling trust score (0–1) based on cumulative policy adherence. Low-trust agents trigger stricter enforcement.

**Quality gates** — configurable score thresholds that trigger holds or blocks when eval scores drop below baseline.

---

### M4 — EvalGov Intelligence

The conversational interface to the entire control plane. Ask anything about any agent, any module, any time period — in natural language.

**Coordinator + sub-agent pattern:** The EvalGov coordinator routes your question to the right specialist sub-agent (M1 eval agent, M2 governance agent, or M3 gateway agent), synthesises the response, and returns a unified answer. Cross-module queries chain multiple sub-agents automatically.

**Proactive monitor** — a background loop runs 15 checks every 60 seconds across governance signals, gateway metrics, and quality/cost trends. When an anomaly is detected, an LLM generates a root cause analysis and stores it as a finding. Findings appear in the portal and are surfaced at the start of EvalGov conversations.

**MCP server** — exposes the coordinator's tools via the Model Context Protocol, so Claude Code, Claude Desktop, and any MCP-compatible client can query the control plane directly.

---

## Deploy

### Prerequisites
- Docker + Docker Compose
- `ANTHROPIC_API_KEY` (for eval judges and all four agents)
- `OPENAI_API_KEY` (if routing to OpenAI models)

### Start the full stack

```bash
cp .env.example .env
# Edit .env — set ANTHROPIC_API_KEY at minimum
make up
open http://localhost:8888
```

This starts the control plane itself. Your own agent system is a separate codebase — see **"Instrument your agents"** below for how to connect it, including how to install the SDK (not on PyPI — installed by path from wherever you just cloned this repo).

### Tiered activation

Start only the modules you need:

| Command | What starts |
|---------|------------|
| `make up-m1` | Gateway + observability + eval (no agents) |
| `make up-m1-m2` | + governance & enforcement |
| `make up-m1-m2-m3` | + gateway agent (M3 sub-agent) |
| `make up` | Full stack — all modules + all agents + portal |

---

## Try it — the bundled demo (opt-demo)

Before instrumenting your own system, see the whole thing working end to end with the included demo: a Google ADK multi-agent system (orchestrator → searcher/summarizer/translator) already wired to all three modules.

```bash
cd opt-demo
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — set OPENAI_API_KEY at minimum
```

If you left `GATEWAY_AUTH_ENABLED` unset in the main `.env` (off by default), that's all you need. If you turned gateway auth on, create a virtual key first and put it in `opt-demo/.env` as both `OPENAI_API_KEY` and `GATEWAY_API_KEY` — see `opt-demo/README.md`'s Prerequisites for the exact command.

Then run it either as an interactive chat UI or as a benchmark server the portal's Eval Testing page can drive:

```bash
streamlit run chat_ui.py       # interactive chat → http://localhost:8501
# or
python3 server.py              # REST API for benchmark runs → http://localhost:8090
```

Try a query like `Search quantum computing, summarize in 20 words, translate to Hindi`, then check `http://localhost:8888` → **M3 Call Log** and **M1 Eval Measurements** — you should see the call and its eval scores appear within seconds. Full walkthrough, including the benchmark-run setup: [`opt-demo/README.md`](opt-demo/README.md).

---

## Instrument your agents

> **Your agent system lives in a different repo/directory than this one.** This repo is the control plane itself (started above via Docker). The SDK packages under `sdk/packages/` are **not published to PyPI** — install them into your agent's environment by local path, pointed at wherever you cloned *this* repo:
> ```bash
> export ACP_REPO=/path/to/this-repo    # wherever you cloned/cd'd into to run `make up` above
> pip install "$ACP_REPO/sdk/packages/acp-sdk"   # umbrella package — installs all four
> ```

**[docs/instrumentation-guide.md](docs/instrumentation-guide.md) is the authoritative, step-by-step guide** — read directly, or hand it to an AI coding agent (it's written to work as a self-contained detect/decide/verify script either way):

```
claude "Follow docs/instrumentation-guide.md to instrument this agent system against the AI Control Plane running at localhost. My agent role is <your-role-name>."
```

It covers the three-layer capture model (gateway wire capture, standards-based tracing, explicit signals), framework-specific notes (OpenAI Agents SDK, Google ADK, LangChain, CrewAI, raw/custom Python), a first-class "how to validate your instrumentation actually worked" section, and a named list of real gotchas hit while building and validating this system — not aspirational advice.

**Fastest path, in brief:** create a virtual gateway key, then point your existing LLM client at the gateway:

```python
import openai
openai.base_url = "http://localhost:8080/v1"
openai.api_key  = "gw-sk-..."          # from the portal → M3 · Agent Gateway → API Keys, or POST /gateway/keys
openai.default_headers = {
    "X-Gateway-Agent-Role": "my-agent",
    "X-Gateway-System-Id":  "my-product",
}
# All existing calls now route through the control plane — no other changes needed
```

That alone gets you full eval scoring and automatic governance with zero other code. For Anthropic/Gemini native protocols, multi-turn conversation scoring, deeper multi-agent structure, or checkpoint/handoff/tool-call signals, see the full guide.

See `sdk/examples/` for full working examples including LangGraph and CrewAI integrations.

---

## Portal

The web portal at **http://localhost:8888** is the operational dashboard for all four modules.

| Module | Pages |
|---|---|
| M1 · Observability & Eval | Eval Testing, Eval Measurements |
| M2 · Governance | AI Governance, Enforcement |
| M3 · Gateway | Gateway Dashboard, Call Log, Routing, Prompt Mods, Shadow Mode, A/B Testing, Changes, API Keys, Traffic Management |
| M4 · Intelligence | EvalGov Agent |

See **[docs/user-guide.md](docs/user-guide.md)** for a full walkthrough of every portal page and what actions you can take.

---

## EvalGov — conversational control plane access

The EvalGov agent at **http://localhost:8888** (portal → EvalGov Agent) or via MCP gives you natural language access to everything:

```
"What agents are running and what are their trust scores?"
"Show me the eval scores for the summarizer agent over the last 24 hours"
"Which A/B test is currently running and who is winning?"
"The translator agent is throwing errors — what's going on?"
"Create a traffic pool with two endpoints and a fallback chain strategy"
"Are there any open circuit breakers or pending HITL requests?"
```

See **[docs/evalgov-playbook.md](docs/evalgov-playbook.md)** for a full playbook of example conversations organized by scenario.

---

## MCP connect

```bash
claude mcp add evalgov --transport sse http://localhost:8003/mcp/sse
```

---

## All endpoints

| Service | URL |
|---|---|
| Portal | http://localhost:8888 |
| Agent Gateway | http://localhost:8080 |
| EvalGov Coordinator / MCP | http://localhost:8003 |
| Eval Runner | http://localhost:8000 |
| Governance Service | http://localhost:8002 |
| Jaeger trace UI | http://localhost:16686 |
| ClickHouse HTTP | http://localhost:8123 |

---

## Repository structure

```
modules/
  m1-observability-eval/     Eval runner (OTLP ingestion, 68-metric pipeline) + eval agent
  m2-governance-enforcement/ Governance service (policy engine, HITL, CBs) + governance agent
  m3-agent-gateway/          Agent gateway (proxy, routing, cache, A/B) + gateway agent
  m4-intelligence/           EvalGov coordinator + proactive monitor + MCP server
portal/                      Streamlit web portal (all four modules)
sdk/                         Instrumentation SDK
  packages/
    acp-gateway/             M3 routing client — OpenAI/Anthropic drop-in
    acp-tracing/             M1 OTLP tracing — 68-metric auto-evaluation
    acp-governance/          M2 policy checks, HITL, circuit breakers
    acp-intelligence/        M4 EvalGov conversational client
    acp-sdk/                 Umbrella — all four packages
  examples/                  basic, multi-agent, LangGraph, CrewAI
opt-demo/                    Pre-instrumented ADK demo (orchestrator, searcher, summarizer, translator)
infra/                       ClickHouse schemas, OTel collector config
docs/                        Instrumentation guide, user guide, EvalGov playbook, operator runbook
```

---

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | LLM eval judges and all four agents |
| `OPENAI_API_KEY` | If using OpenAI | Gateway forwarding and demo agents |
| `AGENT_MODEL` | Optional | LLM for all agents (default: `anthropic/claude-sonnet-4-6`) |
| `GATEWAY_MASTER_KEY` | Recommended | Protects all gateway admin endpoints |
| `GATEWAY_AUTH_ENABLED` | Optional | `true` to require virtual API keys on all requests |
| `GATEWAY_CACHE_TTL_SECONDS` | Optional | Exact-match cache TTL (0 = off) |
| `GATEWAY_SEMANTIC_CACHE_TTL_SECONDS` | Optional | Semantic cache TTL (0 = off) |
| `MONITOR_POLL_SECONDS` | Optional | EvalGov monitor poll interval (default: 60) |
| `HITL_TIMEOUT_MINUTES` | Optional | Minutes before pending HITL creates a finding (default: 15) |

---

## License

[MIT](LICENSE) © 2026 Raj Ramanujam
