# M3 — Agent Gateway

M3 is a transparent LLM proxy that sits between agent clients and LLM providers, enforcing governance at the network edge without requiring changes to agent code. Agents point their `OPENAI_BASE_URL` at the gateway and nothing else changes.

## What it does

### Request routing
- Proxies all LLM API calls (OpenAI, Anthropic, Ollama, Gemini, Bedrock) via LiteLLM
- Model routing policies: rewrite the requested model to a different target based on agent role or system ID, with optional fallback model on upstream failure
- Per-policy fallback: if the primary model fails, automatically retries with a configured fallback model

### A/B testing
- Traffic-split experiments: route a configurable percentage of requests to variant B (different model, prompt, or backend)
- Variant B can be any supported backend including local Ollama models (e.g. `ollama/nemotron-3.5-lightning:latest`)
- Per-variant call stats tracked in ClickHouse; results visible in portal
- Post-routing model allowlist check: if a virtual key restricts allowed models, A/B variant B is also validated against the allowlist

### Shadow mode
- Duplicate any request to a secondary model in the background without affecting the primary response
- Shadow responses are logged and evaluated but never returned to the caller

### Response caching
Two independent cache layers, both disabled by default:

**Exact-match cache** (`GATEWAY_CACHE_TTL_SECONDS`)
- SHA-256 of model + full messages JSON; only hits on byte-for-byte identical requests
- Useful for repeated identical prompts within a TTL window

**Semantic cache** (`GATEWAY_SEMANTIC_CACHE_TTL_SECONDS`)
- Embeds the last user message via `text-embedding-3-small` (configurable), computes cosine similarity against all cached embeddings
- Returns a cached response if similarity ≥ threshold (default 0.92); handles paraphrased queries
- Skipped automatically for agentic mid-loop calls (requests containing tool results) and for responses that include tool calls — prevents agents from looping on cached intermediate steps
- Configurable: threshold, max entries, embedding model

Both caches are visible in the Gateway Dashboard Cache Inspector (unified table showing type, query, model, TTL, token counts, and response preview). Flush buttons available per cache type.

### Virtual API keys
- Issue `gw-sk-*` virtual keys to agents — keys are validated at the gateway, never forwarded upstream
- Per-key controls: allowed model list, daily token limit, rate limit (RPM), budget alert with webhook
- Key editing: update allowed models, limits, and alerts without revoking and reissuing
- Key validation cached for 60 s to avoid DB round-trips on every call
- Events logged to `gateway_key_events` on rate limit, budget, or model allowlist violations

### Prompt modification
- Inject system prompt prefixes, suffixes, or few-shot examples per agent role and system ID
- Applied after routing resolution, before forwarding

### Traffic management (endpoint pools)

Load-balance traffic across a named group of LLM endpoints using one of six strategies:

| Strategy | Behaviour |
|---|---|
| `round_robin` | Rotate through endpoints evenly (default) |
| `weighted` | Probabilistic selection proportional to each endpoint's weight |
| `least_latency` | Always route to the endpoint with the lowest average latency (last 5 min) |
| `performance` | Route to the endpoint with the highest faithfulness eval score (last 1h) |
| `cost_optimized` | Prefer cheapest model; use expensive endpoints only as overflow |
| `fallback_chain` | Try endpoints in priority order (1 = first); move to next on failure |

**Concepts:**

- **Pool** — a named group of endpoints with a chosen strategy. Each endpoint has a `model`, `backend`, `weight` (for `weighted`), and `priority` (for `fallback_chain`).
- **Traffic policy** — binds a `pool_id` to an `agent_role` / `system_id` scope. Traffic policies take precedence over A/B tests and routing policies for the same agent role.
- **Sticky sessions** — optional flag on the policy: pins a conversation (`trace_id`) to the same endpoint for its full duration.

**Routing priority (first match wins on each request):**
1. Traffic policy → pool selection
2. A/B test (if a running test matches)
3. Routing policy (static model override)
4. Passthrough (model_requested unchanged)

**Call log:** every pool-routed call records `routing_reason = pool:<pool_id_prefix>:<endpoint_id_prefix>`, making it trivial to trace which endpoint handled a specific call.

### Edge enforcement
- Calls M2 governance-service synchronously on every request (phase 2 enforcement)
- Blocks or HITL-escalates requests that fail policy checks before they reach the LLM
- Enforcement result recorded on every call log entry

### Observability
- Every request logged to ClickHouse with: model requested, model used, backend, routing reason, cache hit, fallback used, token counts, latency, enforcement result, A/B variant, key ID, pool ID
- Call log filterable in the portal by agent role, system, model, status, cache hit

## Portal pages

| Page | What you can do |
|------|----------------|
| Gateway Dashboard | Live traffic metrics (time-windowed), enforcement status, circuit breakers, cache inspector |
| Call Log | Full request history with filters |
| Routing | Create / delete routing policies with fallback |
| Prompt Mods | Create / delete prompt injections |
| Shadow Mode | Create / delete shadow rules |
| A/B Testing | Create experiments, view per-variant results |
| Changes | Proposed change log |
| API Keys | Create, view, edit, revoke virtual gateway keys |
| Traffic Management | Create pools, add endpoints, set traffic policies, view live per-endpoint stats |

### Traffic Management portal

The **Traffic Management** page has three tabs:

- **🗂️ Pools** — view all pools with their endpoints and linked policy. Delete a pool or policy, or add an endpoint inline.
- **➕ New Pool** — unified creation form: pool settings, endpoint rows (dynamic), and traffic policy in one submit.
- **📊 Live Stats** — per-endpoint call counts, average latency, total tokens, and error rate. Time window selector: 1 hr / 6 hrs / 12 hrs / 24 hrs / 48 hrs / 1 week.

## Dependencies

Requires M1 + M2 (ClickHouse, eval-runner, governance-service).

## Run with M1 + M2

```bash
make up-m1-m2-m3
```

## Pointing agents at the gateway

Set one environment variable before starting your agents:

```bash
export OPENAI_BASE_URL=http://localhost:8080/v1
```

For virtual key auth (when `GATEWAY_AUTH_ENABLED=true`):

```bash
export OPENAI_API_KEY=gw-sk-<your-virtual-key>
```

## Instrumenting external agents

Use the `acp-gateway` SDK to route any external agent's LLM calls through M3:

```bash
# Not published to PyPI — install by path (see docs/instrumentation-guide.md "Prerequisites")
pip install "/path/to/this/repo/sdk/packages/acp-gateway[openai]"
```

See [`sdk/packages/acp-gateway/README.md`](../../sdk/packages/acp-gateway/README.md) for full usage.

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `GATEWAY_MASTER_KEY` | _(empty)_ | Admin key for all write endpoints |
| `GATEWAY_AUTH_ENABLED` | `false` | Require virtual API keys on all requests |
| `GATEWAY_CACHE_TTL_SECONDS` | `0` | Exact-match cache TTL in seconds (0 = off) |
| `GATEWAY_SEMANTIC_CACHE_TTL_SECONDS` | `0` | Semantic cache TTL in seconds (0 = off) |
| `GATEWAY_SEMANTIC_CACHE_THRESHOLD` | `0.92` | Cosine similarity cutoff for semantic cache |
| `GATEWAY_SEMANTIC_CACHE_MAX_ENTRIES` | `500` | Max entries in semantic cache |
| `GATEWAY_EMBED_MODEL` | `text-embedding-3-small` | Embedding model for semantic cache |
| `GATEWAY_FORWARD_TIMEOUT_SECONDS` | `60` | Upstream LLM timeout |
| `OLLAMA_BASE_URL` | `http://localhost:11434` | Ollama endpoint for local model routing |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | OpenAI-compatible endpoint (override to use a custom backend) |

## Traffic management API

| Method | Path | Description |
|--------|------|-------------|
| GET | `/gateway/traffic/pools` | List all pools with member endpoints |
| POST | `/gateway/traffic/pools` | Create a pool `{name, strategy, description}` |
| DELETE | `/gateway/traffic/pools/{pool_id}` | Disable a pool |
| POST | `/gateway/traffic/pools/{pool_id}/endpoints` | Add endpoint `{model, backend, weight, priority}` |
| DELETE | `/gateway/traffic/endpoints/{endpoint_id}` | Remove an endpoint |
| GET | `/gateway/traffic/policies` | List all traffic policies |
| POST | `/gateway/traffic/policies` | Create policy `{agent_role, system_id, pool_id, sticky}` |
| DELETE | `/gateway/traffic/policies/{policy_id}` | Disable a policy |
| GET | `/gateway/traffic/stats?hours=N` | Per-endpoint call stats for pool-routed traffic |

## M3 sub-agent (gateway-agent)

In addition to the agent-gateway service, M3 includes a domain-specific LLM agent that the EvalGov coordinator delegates to for all gateway-related questions and actions.

**What it handles (~33 tools):** gateway call stats, routing decisions, change proposals, A/B tests (list/create/stop/delete/results), shadow vs primary comparison, shadow rules (list/create/delete), prompt mods (list/create/delete), routing policies (list/create/delete), API keys (list/create/update/revoke), key rejection events, **traffic management** — pools (list/create/delete), pool endpoints (add/remove), traffic policies (list/create/delete), live pool traffic stats.

The sub-agent is stateless — it receives a query + optional history, runs its own LLM tool loop, and returns a plain-text response. It starts automatically as part of `make up` and requires `agent-gateway` to be healthy first.

**Example natural language operations via EvalGov:**
- *"Create a round-robin pool for the summarizer with claude-sonnet and gpt-4o-mini"*
- *"Show me live stats for the translator pool over the last 6 hours"*
- *"Remove the ollama endpoint from the test pool — it has a 50% error rate"*
- *"Switch the summarizer pool strategy to least_latency"*

## Key ports

| Service                      | Port |
|------------------------------|------|
| agent-gateway                | 8080 |
| gateway-agent (M3 sub-agent) | 8005 |
| portal                       | 8888 |

---

## Gateway-primary ingest — protocol completeness + checkpoint/handoff/tool-span

M3 is the single front door for every signal the control plane ingests — LLM
traffic in any of four provider dialects, plus the non-LLM signals (pre-action
gates, sub-agent handoffs, tool executions) that never cross the LLM wire. M1
does not require an in-process tracer to produce eval scores for
gateway-routed traffic — it ingests directly from `gateway_call_log` and
`gateway_structural_events`.

`/v1/messages` and Gemini `generateContent` auto-prefix bare model names with
the correct backend (`_ensure_backend_prefix()` in `protocol_adapters.py`) —
real Anthropic/Gemini SDKs send unprefixed model names, and this endpoint
resolves the provider for you rather than requiring the ACP-internal
`anthropic/`/`gemini/` prefix convention used on `/v1/chat/completions`.

### Protocol-complete LLM proxy endpoints

Each of these normalises the provider-native request into the gateway's
internal chat/completions shape, runs the **same** auth/routing/A-B/traffic-
pool/enforcement/cache/logging core as `/v1/chat/completions`, then
translates the response back into the caller's own dialect. No frameworks
were special-cased — any client speaking one of these four wire protocols is
captured automatically.

| Endpoint | Protocol | Unlocks |
|---|---|---|
| `POST /v1/chat/completions` | `openai.chat` | (existing) |
| `POST /v1/responses` | `openai.responses` | OpenAI Agents SDK default transport, hosted tools (WebSearchTool, FileSearchTool, ComputerTool) |
| `POST /v1/messages` | `anthropic.messages` | Claude Agent SDK, native Anthropic SDK, LangChain-Anthropic |
| `POST /v1beta/models/{model}:generateContent` | `google.generateContent` | Google ADK native, Gemini SDK |
| `POST /v1/embeddings` | `embedding` | RAG pipelines, semantic-cache parity |

Streaming variants of `/v1/responses` and `:streamGenerateContent` currently
pass through the underlying chat-completions SSE stream unmodified rather
than re-dialecting each chunk — full per-chunk dialect translation is a
follow-up (see Known limitations below).

`gateway_call_log.protocol` records which dialect each call arrived in, so
the portal Call Log and M1's eval pipeline can filter/group by it.

### Checkpoint / handoff / tool-span endpoints

Same virtual-key auth as the LLM proxy endpoints; write to
`otel.gateway_structural_events` (`call_type` = `checkpoint` | `handoff` |
`tool_span`). These exist because pre-action gates, sub-agent handoffs, and
non-LLM tool calls never cross the LLM wire, so no passive capture — gateway
or OTel — can ever see them; they need an explicit call through the same
front door as everything else.

**`POST /v1/checkpoint`** — pre-action governance gate. Fronts the same
`gate_check()` / HITL flow the inline LLM-call enforcement path already uses
— no new decision logic.

```jsonc
// request
{
  "action": "send_email", "risk_level": "high",
  "system_id": "my-system", "agent_role": "orchestrator",
  "conversation_id": "conv-123", "run_id": "run-456",
  "metadata": {"recipient": "customer@example.com"}
}
// response
{"decision": "allow" | "block" | "hitl_pending", "checkpoint_id": "uuid", "reason": "..."}
```

**`POST /v1/handoff`** — fire-and-forget, marks a sub-agent transition.

```jsonc
{
  "from_agent": "orchestrator", "to_agent": "searcher",
  "conversation_id": "conv-123", "run_id": "run-456",
  "context_summary": "delegating web search for query X"
}
```

**`POST /v1/tool-span`** — fire-and-forget, marks a non-LLM tool execution.

```jsonc
{
  "tool_name": "web_search", "input": {"query": "..."}, "output": "...",
  "status": "ok", "latency_ms": 340,
  "agent_role": "searcher", "conversation_id": "conv-123", "run_id": "run-456"
}
```

### Extended `gateway_call_log` schema (v4 additions)

| Column | Type | Purpose |
|---|---|---|
| `conversation_id` | String | populated from `X-Gateway-Conversation-Id`; multi-turn grouping key for M1 |
| `protocol` | LowCardinality(String) | which endpoint/dialect handled the call (see table above) |
| `started_at` / `ended_at` | DateTime64(3) | real call wall-clock timing (not async-emit timing) |
| `messages_json` | String (≤32KB) | full normalised message array, incl. tool calls/results — feeds M1 target synthesis |
| `response_tool_calls_json` | String (≤8KB) | tool calls the model emitted, if any |

All new columns have defaults — existing rows and readers are unaffected.

### `emit_call_span` demotion

`telemetry.emit_call_span` (span name `gateway.llm_call`) is **not an
eval-trigger input**. M1's ingest pipeline reads `gateway_call_log` directly
instead. The span is still emitted for Jaeger trace visualization only.

### Known limitations / TODOs (this pass)

- Streaming `/v1/responses` and `:streamGenerateContent` pass through the
  chat-completions SSE shape rather than re-dialecting each chunk.
- `/v1/embeddings` uses a lean auth-only path (`ProxyHandler.authenticate()`)
  rather than the full routing/governance/cache core — appropriate since
  embeddings carry no prompt-injection/generation-quality surface, but it
  means routing policies and A/B tests don't apply to embedding calls.
- `/v1/checkpoint`'s HITL wait reuses the existing `wait_for_hitl()`
  synchronous poll (same as inline LLM-call enforcement) rather than
  returning `hitl_pending` immediately and requiring a separate poll —
  simpler for this pass, revisit if checkpoint latency matters.
