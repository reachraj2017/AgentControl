# Instrumentation Guide

How to connect any multi-agent system — yours, or one you're pointing an AI coding agent at — to the AI Control Plane. Works equally well read by a human or followed step-by-step by an AI assistant (Claude Code, etc.); the "AI agent walkthrough" at the end gives it a self-contained detect/decide/verify script.

**This guide reflects the actual, verified v4 architecture** — not aspirational design intent. Where something is designed but not yet confirmed working (e.g. Gemini routing), that's called out explicitly. See `design/v4-implementation-status.md` for the full record of what was built, run for real, and fixed.

---

## Prerequisites

The control plane stack must already be running before you touch any of this — see the root `README.md`'s Quick Start (`cp .env.example .env`, set `ANTHROPIC_API_KEY`, `make up`). Confirm it's up: `curl -sf http://localhost:8080/health && curl -sf http://localhost:8000/health` should both succeed.

Your agent system almost certainly lives in a **different directory/repo** than this one. The SDK packages below aren't published to PyPI — install them by local path, pointed at wherever you cloned *this* repo (see the full package list and options in "SDK packages reference", further down):
```bash
export ACP_REPO=/path/to/this-control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-sdk"   # umbrella — installs all of them
```

**Gateway auth is off by default.** `.env.example` doesn't set `GATEWAY_AUTH_ENABLED`, and `docker-compose.yml` defaults it to `false` (open/dev mode) when unset — so on a fresh clone, Step 1 below (creating a virtual key) is optional, and any `gw-sk-*`-shaped string will be accepted. Only do Step 1 if you've deliberately set `GATEWAY_AUTH_ENABLED=true` (recommended before exposing the gateway beyond `localhost`).

---

## The three-layer model

Three independent layers, increasing depth and effort. Add only what you actually need — most systems only need Layer 1.

| Layer | What it gives you | Effort | When you need it |
|---|---|---|---|
| **1. Gateway wire capture** | Full eval scoring (68-metric pipeline), automatic governance (rate limits, budgets, model allowlists), routing/caching/Call Log | Zero code — point a base URL | Always start here |
| **2. Standards-based tracing** | Deep multi-agent structure: per-sub-agent breakdown, real nested call tree | One line, if your framework emits OTel/OpenInference/OpenLLMetry | You want to see individual sub-agent scores, not just one aggregate per conversation |
| **3. Explicit signals** | Pre-action gates, sub-agent handoffs, non-LLM tool calls — things that never touch an LLM at all | A few explicit function calls at points you control | You need HITL approval gates, or handoff/tool-selection scoring |

Full architectural rationale: `design/v2-gateway-capture-m1-ingest.md` (Layer 1) and `design/checkpoint-handoff-ingest.md` (Layer 3).

---

## Layer 1 — Gateway wire capture

### Step 1 — Create a virtual gateway key (only if you've enabled auth)

Required only when you've explicitly set `GATEWAY_AUTH_ENABLED=true` — see Prerequisites, above; it's off by default. `GATEWAY_MASTER_KEY` is the admin key from your `.env`.

```bash
curl -X POST http://localhost:8080/gateway/keys \
  -H "Authorization: Bearer $GATEWAY_MASTER_KEY" -H "Content-Type: application/json" \
  -d '{"description":"my-new-system","agent_role":"*","system_id":"my-new-system"}'
# → {"key_id": "...", "key": "gw-sk-...", "message": "Save this key — it will not be shown again."}
```

Optional fields on that request: `allowed_models` (list, restricts the key to specific models), `daily_token_limit`, `rate_limit_rpm`, `budget_alert_usd`, `alert_webhook_url`. Admin auth accepts either an `Authorization: Bearer` header or `X-Gateway-Admin-Key`.

### Step 2 — Point your LLM client at the gateway

The endpoint depends on which wire protocol your client speaks — all four are protocol-complete and normalize into the same call record:

| Your client calls... | Point it at | Bare model name resolves to |
|---|---|---|
| OpenAI Chat Completions | `http://localhost:8080/v1/chat/completions` | OpenAI (backward-compatible default) |
| OpenAI Responses API (Agents SDK default transport, hosted tools) | `http://localhost:8080/v1/responses` | OpenAI |
| Anthropic Messages API | `http://localhost:8080/v1/messages` | Anthropic — auto-prefixed for you |
| Gemini `generateContent` | `http://localhost:8080/v1beta/models/{model}:generateContent` | Gemini — auto-prefixed for you |
| Embeddings | `http://localhost:8080/v1/embeddings` | — |

```python
import openai
openai.base_url = "http://localhost:8080/v1"
openai.api_key  = "gw-sk-..."   # from Step 1
```

**OpenAI Agents SDK — set the environment variable, not the module attribute:**
```bash
export OPENAI_BASE_URL="http://localhost:8080/v1"
export OPENAI_API_KEY="gw-sk-..."
```
The SDK builds its own `AsyncOpenAI()` client internally and reads `os.environ["OPENAI_BASE_URL"]` — it does not read `openai.base_url` as a module attribute. Setting the attribute silently does nothing: every call bypasses the gateway with no error, no warning, and zero gateway call-log entries. This was the first thing that broke when this was tested against a real external agent system (`docs/external-agent-integration-findings.md`, Issue 1) — worth calling `agents.set_default_openai_client()` explicitly if you want to be certain, and `agents.set_tracing_disabled(True)` to suppress the SDK's own competing telemetry (Issue 5, same doc).

**Anthropic/Gemini native clients** — just point `base_url` at the gateway's `/v1/messages` or the Gemini path with your normal, unprefixed model name (e.g. `claude-sonnet-4-6`, not `anthropic/claude-sonnet-4-6`) — the gateway resolves the provider automatically for these protocol-specific endpoints.

### Step 3 — Add identity headers

```
X-Gateway-Agent-Role: <role, e.g. "researcher">
X-Gateway-System-Id: my-new-system
X-Gateway-Conversation-Id: <a stable id per multi-turn session>
```

Give each distinct agent/role in your system its own `X-Gateway-Agent-Role` — one shared role for everything makes every agent indistinguishable in the Call Log and in EvalGov. `X-Gateway-Conversation-Id` matters specifically for **multi-turn conversation scoring** (knowledge retention, role adherence, conversation completeness/relevancy) — without it, each turn is scored independently and conversation-level scores never appear (they require ≥2 turns sharing one conversation ID and 60 seconds of idle time to fire — see Validation, below).

That's the whole floor. No code inside your agent process beyond the base-URL redirect and headers.

---

## Layer 2 — Standards-based tracing (optional)

If your framework already emits OTel spans natively, or has a community [OpenInference](https://github.com/Arize-ai/openinference) or [OpenLLMetry](https://github.com/traceloop/openllmetry) auto-instrumentor available, point it at the collector:

```python
from acp_tracing.otel_ecosystem import configure_otlp_exporter
configure_otlp_exporter(endpoint="http://localhost:4318")

# then, e.g.:
from openinference.instrumentation.openai import OpenAIInstrumentor
OpenAIInstrumentor().instrument()
# or: from traceloop.sdk import Traceloop; Traceloop.init(app_name="my-agent", api_endpoint="http://localhost:4318")
```

M1's ingestion normalizes OTel GenAI, OpenInference, and OpenLLMetry semantic conventions natively (`modules/m1-observability-eval/eval_runner/ingestion/semconv_mapping.py`) — you don't need ACP's own tracer for this to trigger evaluation.

**Or**, for the richer ACP-native path (what the bundled `opt-demo` uses):
```python
from acp_tracing import instrument
instrument(service_name="my-agent", agent_role="orchestrator",
           otlp_endpoint="http://localhost:4318", system_id="my-new-system")
```
This registers a global OTel `TracerProvider`; any framework using OTel's global provider (ADK, LangChain, LlamaIndex) then attaches automatically. Point it at the collector (`:4318`, not eval-runner directly) to also get Jaeger trace visualization for free — the collector fans out one batch to ClickHouse, Jaeger, and eval-runner's evaluation trigger in parallel.

For manual, fine-grained spans on top of `instrument()` (useful for a raw/custom agent loop with no framework-native tracing at all), `acp_tracing` also exposes:

```python
from acp_tracing import ACPTracer   # lower-level than instrument() — construct directly if you need per-call control

tracer = ACPTracer(otlp_endpoint="http://localhost:4318", agent_name="my-agent",
                    agent_role="researcher", system_id="my-new-system")

with tracer.span(model="gpt-4o-mini", conversation_id=conv_id) as ctx:
    ctx.prompt = prompt_text
    result = call_llm(prompt_text)
    ctx.completion = result
    ctx.tokens_in, ctx.tokens_out = usage.input_tokens, usage.output_tokens
# span is exported automatically on exit, success or exception

# or the decorator form:
@tracer.trace(model="gpt-4o-mini")
def call_llm(prompt: str) -> str: ...

# or record a call you've already made, after the fact:
tracer.record_call(model="gpt-4o-mini", prompt=prompt_text, completion=result,
                    tokens_in=115, tokens_out=30, latency_ms=820.0)
```

And `AgentSpan` — a lighter context manager for a framework (like ADK) where a global OTel provider is already configured via `instrument()`, when you just want to wrap one operation and tag it:
```python
from acp_tracing import AgentSpan

with AgentSpan("process_query", agent_role="researcher", llm_model="gpt-4o-mini") as span:
    result = call_llm(prompt)
    span.set_attribute("gen_ai.completion", result)
    span.set_llm_tokens(tokens_in=512, tokens_out=128)
```

**Gotcha if you combine this with manual ACP-native spans for the same call** (i.e. your framework has its own native tracing *and* you also wrap calls manually): a framework's own wrapper span (e.g. Google ADK's `invoke_agent <agent>`) can be a nested, redundant representation of the same invocation your manual span already covers. M1's ingestion now deduplicates this correctly — only the outermost/native span counts per invocation — but it was a real bug found and fixed this session (`design/v4-implementation-status.md` §4.5/§4.6), so if you see a phantom `agent_name: unknown` row or a token count that looks inflated relative to what the gateway itself logged for the same call, that combination is the first thing to check.

---

## Layer 3 — Explicit signals (optional)

For pre-action gates, sub-agent handoffs, and non-LLM tool calls — none of which cross the LLM wire, so no passive capture (gateway or OTel) can ever see them:

```python
from acp_signals import context, checkpoint, handoff, tool_span

context.set(conversation_id="conv-123", system_id="my-new-system", agent_role="orchestrator")

decision = checkpoint("send_email", risk_level="high", metadata={"to": "user@example.com"})
if decision.decision == "block":
    raise PermissionError(decision.reason)

handoff("orchestrator", "researcher", context_summary="delegating web search for query X")

tool_span("web_search", input={"query": "..."}, output={"hits": 5}, latency_ms=210)
```

`checkpoint()` blocks (it's a real gate — forwards to M2 governance synchronously); `handoff()`/`tool_span()` are fire-and-forget on a background thread. Reads `ACP_GATEWAY_URL`/`GATEWAY_API_KEY` by default, same convention as `acp-gateway`.

### Framework adapters

Each adapter wires a framework's own **documented** hook/callback/plugin interface to the three calls above — never an undocumented internal. Confidence levels below were verified against real installed (or, for CrewAI, freshly downloaded) package source — see `sdk/packages/acp-signals/README.md` for the up-to-date table and `design/v4-implementation-status.md` §5 for how each was checked:

| Framework | Adapter | Confidence |
|---|---|---|
| LangChain / LangGraph | `acp_signals.adapters.langchain.ACPCallbackHandler` | Verified against `langchain-core==1.6.2` |
| OpenAI Agents SDK | `acp_signals.adapters.openai_agents.ACPRunHooks` | Verified against `openai-agents==0.22.2` |
| Google ADK | `acp_signals.adapters.google_adk` | Verified against `google-adk==2.8.0` |
| CrewAI | `acp_signals.adapters.crewai` | Verified against `crewai==1.15.21` |
| Raw / custom Python | — | Call `checkpoint()`/`handoff()`/`tool_span()` directly at the relevant lines — see `sdk/examples/custom_agent_signals.py` |

If you're on a materially different version of any of these frameworks, re-verify against that adapter's module docstring — it documents exactly which hook signature was assumed and what to check.

---

## Governance (M2) — automatic, check its posture

Rate limits, budgets, and model allowlists on your virtual key apply the moment you route through the gateway — no extra step. Enforcement is **fail-open by default** per category (a policy failure lets the call through rather than blocking it) — check the portal's Enforcement page to confirm this matches what you expect for each category, especially anything safety- or compliance-sensitive.

---

## Validate — this is not optional

**Every non-trivial bug found in this system, across an extensive validation pass, was found by running one real request and comparing raw data — never by reading code alone.** Do this after wiring anything, before trusting it:

1. Send one real request through your new integration.
2. Check the portal's **Call Log** (M3) — did it show up, with the right `agent_role`/`system_id`, and correct tokens?
3. Check **Eval Measurements** / **Prompt Analysis** (M1) — did scores appear, one row per agent, within a few seconds?
4. **Compare token counts** between the Call Log entry and the corresponding Prompt Analysis row for the *same call*. This single comparison catches almost every real instrumentation bug: a mismatch means something is duplicating, misattributing, or dropping data upstream of scoring.
5. If multi-turn: wait past 60 seconds of conversation idle time and check for a separate conversation-level score (requires ≥2 turns sharing the same `X-Gateway-Conversation-Id`).
6. If anything looks duplicated, missing, or inflated, that token-count comparison from step 4 is where to start — not the code.

Direct ClickHouse queries if you want to check without the portal:
```sql
SELECT agent_role, tokens_in, tokens_out FROM otel.gateway_call_log WHERE trace_id = '<your trace_id>';
SELECT agent_name, prompt_tokens, completion_tokens FROM otel.prompt_evals WHERE trace_id = '<your trace_id>';
```

---

## Common gotchas (all real, all hit during this system's own validation)

- **OpenAI Agents SDK base-URL redirect** — must be an environment variable, not a module attribute (Layer 1, Step 2, above).
- **Native protocol model prefixing** — handled automatically by the gateway now; if you're checking behavior against an older deployment, confirm `protocol_adapters.py` has `_ensure_backend_prefix`.
- **Dual-instrumentation nested spans** — see the Layer 2 gotcha above.
- **Conversation ID required for multi-turn scoring** — without `X-Gateway-Conversation-Id` (or the equivalent explicit signal), you'll only ever see per-turn scores, never conversation-level ones.
- **Traffic pools can route to surprising backends.** If you set up a weighted traffic pool that includes a local Ollama endpoint, expect that model's own answers and its own token-accounting quirks (some models report internal reasoning tokens in their usage stats even though that content never appears in the visible response) — check `model_used`/`backend_used` on the Call Log entry before assuming an eval score reflects your primary model.
- **Gemini `generateContent` routing was fixed but not confirmed against a real Gemini API key** in this environment — the model-prefix logic was verified via LiteLLM's error message changing from a routing failure to a credentials failure, but no successful real Gemini response was observed. Validate this one yourself if you use it.

---

## AI-agent walkthrough

If you're an AI coding agent (or a human) instrumenting an unfamiliar target repo, work through this in order:

1. **Detect the framework.** Grep the target for `from agents import` (OpenAI Agents SDK), `from google.adk` (ADK), `from langchain`/`from langgraph`, `from crewai`, or direct `openai.OpenAI()`/`anthropic.Anthropic()` construction. Check `requirements.txt`/`pyproject.toml` too.
2. **Confirm the control plane is reachable** before touching anything: `curl -sf http://localhost:8080/health && curl -sf http://localhost:8000/health && curl -sf http://localhost:8002/health`. If any is unreachable, stop and tell the user to `make up` first — proceeding anyway fails silently later with no clear signal why.
3. **Check whether gateway auth is enabled** (`GATEWAY_AUTH_ENABLED` in `.env` — off by default). If it's on, create a virtual key (Layer 1, Step 1) scoped to a `system_id` matching the target repo's name; if it's off, skip straight to Step 4 with any `gw-sk-*`-shaped placeholder string.
4. **Wire Layer 1 only, first.** Point the target's LLM client at the gateway (Step 2/3 above, with the framework-specific env-var gotcha if it's OpenAI Agents SDK). Do not add Layer 2 or 3 yet.
5. **Send one real request and validate** (see Validate, above) before adding anything else. If the Call Log and Eval Measurements both show the call correctly, Layer 1 is done — stop here unless the user specifically wants deeper structure.
6. **Only if asked for deeper per-sub-agent breakdown**, add Layer 2 — check whether the framework already has an OpenInference/OpenLLMetry instrumentor before writing anything with `acp-tracing` directly.
7. **Only if asked for pre-action gates or explicit handoff/tool scoring**, add Layer 3 with the matching framework adapter (or raw calls if none exists), and confirm it against that adapter's own docstring for the installed framework version — don't assume it's still accurate without checking.
8. **Report exactly what you verified**, not what you configured — "I set X" is not the same claim as "I confirmed X produced a scored call in Eval Measurements." Only claim the latter if you actually checked.

---

## SDK packages reference

| Package | Purpose |
|---|---|
| `acp-gateway` | Convenience client for the gateway — `GatewayClient(...).openai_client()`/`.anthropic_client()`/`.chat_openai()` drop-in wrappers, plus `get_call_stats()`/`get_traffic_pools()` |
| `acp-tracing` | Layer 2 — `instrument()` (ACP-native global tracer) or `otel_ecosystem.configure_otlp_exporter()` (bring-your-own community instrumentor) |
| `acp-governance` | Direct `GovernanceClient` for policy checks outside the gateway's automatic per-call enforcement |
| `acp-signals` | Layer 3 — `checkpoint()`/`handoff()`/`tool_span()` + framework adapters |
| `acp-sdk` | Umbrella package — installs all of the above |

Install by path from wherever you cloned this repo (see Prerequisites, above — not published to PyPI). `$ACP_REPO` below is that path; if you happen to be running this from inside the control-plane repo itself, `.` also works in place of `$ACP_REPO`:
```bash
pip install "$ACP_REPO/sdk/packages/acp-gateway[all]" "$ACP_REPO/sdk/packages/acp-tracing[otel]" \
            "$ACP_REPO/sdk/packages/acp-governance" "$ACP_REPO/sdk/packages/acp-signals"
# or the umbrella: pip install "$ACP_REPO/sdk/packages/acp-sdk"
```

Requires Python 3.11+. `httpx` is the only mandatory dependency; OTel packages are optional (Layer 2 silently no-ops without them).

---

## Environment variables reference

| Variable | Default | Used by |
|---|---|---|
| `ACP_GATEWAY_URL` | `http://localhost:8080` | `acp-gateway`, `acp-signals` |
| `GATEWAY_API_KEY` | *(empty)* | `acp-signals` (also read by `acp-gateway` under its own client config) |
| `ACP_GOVERNANCE_URL` | `http://localhost:8002` | `acp-governance` |
| Collector OTLP endpoint | `http://localhost:4318` | pass explicitly to `instrument()`/`configure_otlp_exporter()` |

For a remote deployment, replace `localhost` with the host running the control plane stack.
