# Design: Gateway Capture → M1 Ingest

**Status:** Implemented and validated in v4 — see [`v4-implementation-status.md`](./v4-implementation-status.md) for what was actually built, tested against a live stack, and fixed. This doc remains accurate as the architecture rationale; treat the status doc as authoritative for current state.
**Date:** 2026-08-26
**Author:** Raj Ramanujam
**Scope:** Local design work.

---

## 1. Problem statement

The control plane has two integration surfaces:

- **M3 (Agent Gateway)** — an inline proxy. Point an agent's `base_url` at it and every LLM call is routed, cached, enforced, and logged.
- **M1 (Observability & Eval)** — an OTLP consumer. It assembles traces, runs 68 metrics, and populates Eval Measurements + Prompt Analysis.

Today these two only work **together** when the caller *also* runs the ACP in-process tracing SDK. The bundled `opt-demo` does exactly that: `opt-demo/runner.py` manually emits `agent.task` spans, ADK emits `generate_content` spans, and `opentelemetry-instrumentation-openai` emits `openai.chat` spans — all exported straight to M1's `POST /v1/traces`. The gateway route is *additional*, correlated only by `trace_id` via the W3C `traceparent` header.

An external agent system that does the documented thing — **"just point `base_url` at the gateway"** — gets:

| Surface | Result |
|---|---|
| M3 Call Log (`gateway_call_log`) | ✅ fully populated |
| M1 Eval Measurements / Prompt Analysis | ❌ empty |

**Root cause:** M1's only trigger is a span **named exactly `agent.task`** (`pipeline/trigger.py:35`, `ingestion/trace_assembler.py:119`). The gateway emits a span named `gateway.llm_call` (`agent_gateway/telemetry.py:47`), flat, async-timed to ~0 ms. M1 silently drops it. The gateway is trying to *impersonate an in-process tracer* and getting the shape wrong, instead of just handing M1 the call data and letting M1 build the trace.

The `openai-cs-agents-demo` integration test (see `docs/external-agent-integration-findings.md`) hit this as Issue 4 and had to hand-patch the gateway to emit an `agent.task` parent + nested `llm_call` child + backdated timestamps + `conversation.id`. That work reconstructs, inside M3, what M1 should be able to derive itself.

---

## 2. Goals / non-goals

### Goals

1. **Gateway-only integration produces full evals.** Route any framework's traffic through M3 → Eval Measurements and Prompt Analysis populate, with no ACP SDK in the target process.
2. **M1 and M3 are peers, not nested.** M3 captures and enforces. M1 evaluates. Neither owns "the flow" — the caller does.
3. **The hand-off is a documented, versioned contract** (a call record), not an implicit span-tree shape.
4. **`opt-demo` and every existing in-process integration keep working unchanged.**
5. **Capture is protocol-complete** enough to actually see all traffic from OpenAI Agents SDK, Claude Agent SDK, and Google ADK.

### Non-goals

- Replacing the in-process tracing SDK. It stays as the richer "Layer 3" path for deep multi-agent structure.
- Full nested sub-agent trace trees from gateway-only data. A flat 2-level trace (conversation → N calls) is acceptable and sufficient for the large majority of metrics.
- Non-Python framework *SDKs*. Since capture is HTTP, the "adapter" for TS/JS/Go is a documented `baseURL` + headers snippet.

---

## 3. Current data flow (as-is)

### 3.1 In-process path (works — this is what `opt-demo` uses)

```
agent process
  ├─ acp_tracing.instrument()  → global OTel TracerProvider → M1 POST /v1/traces
  ├─ runner.py: tracer.start_as_current_span("agent.task")   (manual)
  ├─ ADK: "generate_content <model>" spans                    (auto)
  └─ OpenAIInstrumentor: "openai.chat" spans + traceparent    (auto)
        │
        ▼
M1  /v1/traces → SpanReceiver.parse → repo.save_spans_batch → otel_traces
    trigger.is_completed_task(span)  == "agent.task"?  ─────► _run_eval_in_background (sleep 10s)
        └─ trigger.trigger_evaluation → EvalPipeline.run(trace_id, run_id)
             └─ TraceAssembler.extract_eval_targets(trace_id)
                  → task_spans  (span_name == "agent.task")
                  → llm_spans   (name contains llm / generate_content / chat)
                  → tool_spans, handoff_spans, all_spans
             └─ per task_span: run evaluators → EvalResult[]
             └─ _save_prompt_eval  → otel.prompt_evals   (Prompt Analysis view)
             └─ save eval scores   → otel.eval_scores    (Eval Measurements view)
```

### 3.2 Gateway path (partial — logs, no evals)

```
agent process  (base_url → gateway, NO acp_tracing)
        │  POST /v1/chat/completions
        ▼
M3  proxy.handle()
    ├─ auth / routing / A-B / traffic pool
    ├─ governance gate_check (block / HITL)
    ├─ prompt mods
    ├─ litellm.acompletion → upstream
    ├─ build call_record  (proxy.py:923)
    └─ _emit_async:
         ├─ db.log_call(record)      → otel.gateway_call_log     ✅ durable
         └─ emit_call_span(record)   → OTel "gateway.llm_call"   → otel_traces
                                        └─ M1 trigger ignores it (name != "agent.task")   ❌
```

The `call_record` already contains everything M1 needs:

```python
# agent_gateway/proxy.py:923
call_record = dict(
    call_id, trace_id, run_id, system_id, agent_role,
    model_requested, model_used, backend_used, routing_reason,
    mods_applied, prompt_text, response_text,
    tokens_in, tokens_out, latency_ms,
    enforcement_result, status, is_shadow,
    ab_test_id, ab_variant, key_id, cache_hit, fallback_used,
)
```

### 3.3 Precedent: `ShadowEvalPipeline` already does the target pattern

`modules/m1-observability-eval/eval_runner/pipeline/shadow_eval_pipeline.py` **already reads `gateway_call_log` directly** (`is_shadow=1` rows), runs 3 LLM judges, and writes `gateway_shadow_evals`. It runs on a 60 s background loop (`_shadow_eval_loop` in `main.py`). This design **generalises that proven pattern** to all gateway calls and routes them through the *real* 68-metric pipeline instead of a 3-metric side judge.

---

## 4. Proposed architecture

### 4.1 The contract: `GatewayCallRecord`

A versioned schema — the **only** thing M3 and M1 agree on. It is a superset of today's `call_record` dict plus explicit timing and conversation identity.

```jsonc
{
  "schema_version": "1.0",

  // identity
  "call_id":        "uuid",              // unique per LLM call
  "trace_id":       "hex32 | ''",        // W3C trace id if caller propagated one
  "run_id":         "uuid | ''",         // ACP run/session id if provided
  "conversation_id":"string | ''",       // X-Gateway-Conversation-Id (multi-turn key)
  "system_id":      "string",            // X-Gateway-System-Id
  "agent_role":     "string",            // X-Gateway-Agent-Role (per-call, may vary within a system)

  // request / response (canonical, provider-neutral)
  "protocol":       "openai.chat | openai.responses | anthropic.messages | google.generateContent",
  "model_requested":"string",
  "model_used":     "string",
  "backend_used":   "openai | anthropic | google | ollama | ...",
  "messages":       [ /* normalised chat messages, incl. tool calls + tool results */ ],
  "system_prompt":  "string | ''",
  "response_text":  "string",            // assembled final text (streaming already accumulated)
  "response_tool_calls": [ /* normalised tool calls the model emitted */ ],
  "prompt_text":    "string",            // last user turn, for grouping / quick views

  // metrics
  "tokens_in":      0,
  "tokens_out":     0,
  "latency_ms":     0,
  "started_at":     "iso8601",           // REAL call start (not span-emit time)
  "ended_at":       "iso8601",

  // control-plane metadata (informational for eval, primary for portal)
  "routing_reason": "string",
  "mods_applied":   ["..."],
  "enforcement_result": "pass | blocked | hitl_approved | hitl_rejected",
  "cache_hit":      false,
  "fallback_used":  false,
  "is_shadow":      false,
  "ab_test_id":     "", "ab_variant": "",
  "status":         "ok | error | blocked",
  "error_detail":   ""
}
```

Stored durably in `gateway_call_log` (extended — see §5.1). The table **is** the queue.

### 4.2 Flow (to-be)

```
agent process  (base_url → gateway, ANY framework, ANY language, no ACP SDK)
        │
        ▼
M3  proxy.handle()   ── unchanged control logic ──
        └─ persist GatewayCallRecord → gateway_call_log   (durable, immutable)
        └─ (drop the fake "gateway.llm_call" OTel span, or keep it Jaeger-only)

M1  GatewayIngestPipeline  (background loop, ~5s, generalises ShadowEvalPipeline)
     1. fetch un-ingested rows from gateway_call_log
     2. group by (trace_id | conversation_id | run_id)
     3. if a real in-process trace already exists for trace_id → MERGE mode
        else → SYNTHESISE mode
     4. build eval targets:
          task_spans[]  — one synthetic agent.task per (agent_role change | call)
          llm_spans[]   — one synthetic llm_call per row, real tokens + timestamps
          tool_spans[]  — derived from messages[].tool_calls / role:"tool"
     5. EvalPipeline.run_from_targets(targets, trace_id, run_id, mode="online")
          → otel.eval_scores      (Eval Measurements)
          → otel.prompt_evals     (Prompt Analysis)
     6. mark rows ingested
```

### 4.3 Why this decouples cleanly

| Concern | Owner | Mechanism |
|---|---|---|
| Intercept traffic, capture req/resp | M3 | inline proxy (already) |
| Routing, cache, A/B, key auth, rate limit | M3 | already |
| Governance enforcement (block / HITL / CB) | M3 | already, inline (must be) |
| Durable call log | M3 | `gateway_call_log` (already) |
| Trace assembly | M1 | synthesise from call records **or** merge with in-process spans |
| 68 metrics, LLM judges, sampling | M1 | existing `EvalPipeline`, new entry point |
| Prompt Analysis / regression / benchmarks | M1 | already, fed by `prompt_evals` |

M3 emits an event and moves on. M1 is a subscriber to a durable table. If M1 is down, records accumulate and are processed on recovery — no loss, no backpressure on the request path.

---

## 5. Detailed design

### 5.1 M3 changes

**Minimal — the eval path needs almost nothing from M3 because `gateway_call_log` already exists.**

1. **Extend `gateway_call_log`** (`agent_gateway/db.py` DDL + `log_call`):
   - `conversation_id String DEFAULT ''`
   - `protocol LowCardinality(String) DEFAULT 'openai.chat'`
   - `started_at DateTime64(3)`, `ended_at DateTime64(3)` — real call timing
   - `messages_json String DEFAULT ''` — full normalised message array (for judges that need context / tool info); cap at e.g. 32 KB
   - `response_tool_calls_json String DEFAULT ''`
   - keep `prompt_text` / `response_text` as the truncated quick-view copies
2. **Ingestion state** — do *not* mutate the immutable log. Add a sidecar table:
   ```sql
   CREATE TABLE otel.gateway_call_eval_state (
       call_id     String,
       state       LowCardinality(String) DEFAULT 'pending',  -- pending | done | skipped | error
       trace_id    String DEFAULT '',
       updated_at  DateTime64(3) DEFAULT now64(3)
   ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY call_id;
   ```
   M1 writes this; M3 never reads it.
3. **Populate `conversation_id`** from the `X-Gateway-Conversation-Id` header in `main.py` (currently only `X-Gateway-Run-Id` / `traceparent` are read).
4. **Stop the span impersonation.** `emit_call_span` either:
   - (a) is deleted, or
   - (b) keeps emitting `gateway.llm_call` **only** for Jaeger visualisation, clearly *not* on the eval trigger path.
5. **Protocol completeness** (separate workstream, §5.5) — add `/v1/responses`, native `/v1/messages`, Gemini `generateContent` so "capture all traffic" is literally true for the three target frameworks. Each new endpoint normalises into the same `GatewayCallRecord` and the same `gateway_call_log` insert.

**No change** to routing, enforcement, caching, auth, A/B, traffic pools, streaming.

### 5.2 M1 changes

**New module: `pipeline/gateway_ingest_pipeline.py`** (generalises `shadow_eval_pipeline.py`)

```python
class GatewayIngestPipeline:
    def __init__(self, repository, eval_pipeline, trace_assembler): ...

    def run_batch(self, batch_size=50) -> int:
        rows = self._repo.get_pending_gateway_calls(batch_size)   # LEFT JOIN eval_state WHERE state IS NULL/'pending'
        groups = self._group(rows)      # by trace_id -> conversation_id -> run_id -> call_id
        for key, group_rows in groups.items():
            try:
                if self._repo.trace_has_inprocess_spans(key.trace_id):
                    self._merge(key, group_rows)      # in-process trace wins; attach gw metadata, skip re-eval
                else:
                    targets = self._synthesise_targets(group_rows)
                    self._eval.run_from_targets(
                        targets, trace_id=key.eval_trace_id, run_id=self._resolve_run(group_rows),
                        mode="online",
                    )
                self._repo.mark_gateway_calls(group_rows, "done", key.eval_trace_id)
            except Exception as exc:
                self._repo.mark_gateway_calls(group_rows, "error", "")
        return len(rows)
```

**Target synthesis** — produce exactly the dict shape `TraceAssembler.extract_eval_targets` returns:

```python
def _synthesise_targets(self, rows) -> dict:
    all_spans, task_spans, llm_spans, tool_spans = [], [], [], []
    prev_role = None
    root_id = _mk_id()
    for i, r in enumerate(sorted(rows, key=lambda r: r["started_at"])):
        task_id = root_id if i == 0 else _mk_id()
        task_spans.append({
            "span_id": task_id,
            "parent_span_id": "" if i == 0 else root_id,
            "span_name": "agent.task",
            "status_code": "STATUS_CODE_ERROR" if r["status"] == "error" else "STATUS_CODE_OK",
            "duration_ns": r["latency_ms"] * 1_000_000,
            "attributes": {
                "agent.role":      r["agent_role"],
                "agent.id":        r["agent_role"],
                "task.input":      r["prompt_text"],
                "task.output":     r["response_text"],
                "run.id":          r["run_id"],
                "conversation.id": r["conversation_id"],
                "trace.source":    "gateway",
            },
        })
        llm_id = _mk_id()
        llm_spans.append({
            "span_id": llm_id, "parent_span_id": task_id,
            "span_name": "llm_call",
            "duration_ns": r["latency_ms"] * 1_000_000,
            "attributes": {
                "gen_ai.request.model":       r["model_used"],
                "gen_ai.usage.input_tokens":  r["tokens_in"],
                "gen_ai.usage.output_tokens": r["tokens_out"],
                "gen_ai.prompt":              _messages_to_text(r["messages_json"]),
                "gen_ai.completion":          r["response_text"],
            },
        })
        # tool spans from the message array
        for tc in _extract_tool_calls(r):
            tid = _mk_id()
            tool_spans.append({
                "span_id": tid, "parent_span_id": task_id,
                "span_name": "agent.tool_call",
                "attributes": {"tool.name": tc["name"], "tool.input": tc["args"],
                               "tool.output": tc.get("result", ""), "agent.id": r["agent_role"]},
            })
        # handoff span when the acting role changes between consecutive calls
        if prev_role and prev_role != r["agent_role"]:
            handoff_spans... # span_name "agent.handoff", from=prev_role, to=r["agent_role"]
        prev_role = r["agent_role"]
        all_spans += [task_spans[-1], llm_spans[-1], *tool_spans[-N:]]
    return {"task_spans": task_spans, "llm_spans": llm_spans,
            "tool_spans": tool_spans, "handoff_spans": handoff_spans, "all_spans": all_spans}
```

**`EvalPipeline` refactor** (`pipeline/eval_pipeline.py`)

Extract the body of `_run_inner` after `extract_eval_targets` into:

```python
def run_from_targets(self, targets: dict, trace_id: str, run_id: str, mode="online") -> list[EvalResult]:
    """Evaluate a pre-assembled target set (used by GatewayIngestPipeline)."""
    ...  # identical evaluator cascade + _save_prompt_eval + eval_scores writes

def _run_inner(self, trace_id, run_id, mode, hint_spans):
    targets = self._assembler.extract_eval_targets(trace_id, hint_spans=hint_spans)
    return self.run_from_targets(targets, trace_id, run_id, mode)
```

No change to evaluators, config, sampling, `_save_prompt_eval`, or the ClickHouse writes. Prompt Analysis and Eval Measurements populate through the exact same code path they use today.

**Wiring** (`main.py`)

- Instantiate `GatewayIngestPipeline` in `lifespan`.
- Add `_gateway_ingest_loop()` mirroring `_shadow_eval_loop()` — `await asyncio.sleep(5)` between batches.
- Keep `_shadow_eval_loop` as-is, or fold shadow rows into the same pipeline with `mode` distinguishing them.

**Trigger config fix** (secondary, benefits the in-process path too)

`pipeline/trigger.py` — replace the hard `span.get("span_name") != "agent.task"` with a configurable set:

```yaml
# config/evaluator_config.yaml
ingestion:
  task_span_names: ["agent.task", "invoke_agent", "agent.run"]   # extensible per framework
```

This lets native OTel GenAI spans from LangChain/CrewAI/LlamaIndex (via OpenLLMetry / OpenInference) trigger evals without the gateway at all.

### 5.3 Trace correlation model

| Caller setup | `trace_id` on gateway calls | M1 behaviour |
|---|---|---|
| Gateway only, no propagation | empty | Group by `conversation_id`, else `run_id`, else treat each call as a singleton trace. `eval_trace_id = call_id` or a synthesised id. |
| Gateway only + `X-Gateway-Conversation-Id` | empty | Multi-row synthetic trace per conversation → multi-turn metrics work. |
| Gateway + framework propagates `traceparent` | real hex32 | If in-process spans exist for it → MERGE (in-process trace is authoritative, attach routing/cache/enforcement metadata, do **not** re-run evals). If not → SYNTHESISE under that `trace_id`. |
| Full ACP in-process SDK (`opt-demo`) | real | Unchanged. In-process trigger fires first; gateway rows detected as already-traced → MERGE metadata only. |

**Double-eval guard:** `gateway_call_eval_state` + a `trace_has_inprocess_spans(trace_id)` check (query `otel_traces` for a non-gateway `agent.task` in that trace). MERGE mode never calls `run_from_targets`.

### 5.4 Multi-agent & tool semantics from gateway-only data

Derivable from `gateway_call_log` rows alone:

- **Per-agent attribution** — `agent_role` header per call; each becomes its own `agent.task` row in Prompt Analysis.
- **Ordering / pipeline shape** — `started_at` ordering within a `trace_id` / `conversation_id` group.
- **Handoffs** — inferred when `agent_role` changes between consecutive calls in a group. Good enough for `handoff_fidelity` (did context carry over) but not a literal SDK handoff event.
- **Tool calls** — `messages[]` carries `assistant.tool_calls` and `role:"tool"` results; `response_tool_calls_json` carries the latest. Enough for `tool_selection_accuracy`, `tool_argument_accuracy`, `tool_error_rate`.
- **Cost** — `tokens_in/out` × model rate (M1 already has `_ensure_cost_rates`).

Not derivable without in-process spans (accepted non-goals):

- True nested sub-agent hierarchy depth, `step_efficiency` (needs internal reasoning steps), `context_propagation` across framework-internal calls, `dead_span_rate`.

### 5.5 Protocol completeness (capture workstream)

"Capture all traffic" is only true if the gateway speaks what the frameworks emit:

| Add to `agent_gateway/main.py` + `proxy.py` | Unlocks | Normalises to |
|---|---|---|
| `POST /v1/responses` (+ SSE, tool passthrough) | OpenAI Agents SDK (Py + TS) default transport, hosted tools | `protocol: openai.responses` |
| `POST /v1/messages` native (streaming, tool_use) | Claude Agent SDK, Anthropic SDK, LangChain-Anthropic | `protocol: anthropic.messages` |
| `POST /v1beta/models/{model}:generateContent` + `:streamGenerateContent` | Google ADK native, Gemini SDK | `protocol: google.generateContent` |
| `POST /v1/embeddings` | RAG pipelines; semantic-cache parity | — |

Each handler: parse provider-native body → canonical `messages[]` → existing `proxy.handle` core → forward via LiteLLM (`litellm.aresponses` / `litellm.acompletion` with the right model prefix) → re-serialise the response in the caller's dialect → `GatewayCallRecord` written once, dialect-agnostic.

This workstream is independent of §5.1–5.3 and can land after, but without it the OpenAI Agents SDK and ADK-native paths 404 before any capture happens.

---

## 6. Metric coverage after this change (gateway-only integration)

| Category | Gateway-only | Notes |
|---|---|---|
| Quality (correctness, faithfulness, relevance, coherence, conciseness, hallucination) | ✅ full | prompt + completion is all the judges need |
| Safety (PII, prompt injection, instruction following, role adherence, bias, toxicity) | ✅ full | |
| Performance (latency, tokens, cost, error rate, timeout rate) | ✅ full | real `started_at/ended_at` from M3 |
| Multi-turn (completeness, relevancy, knowledge retention) | ✅ with `conversation_id` | needs the header set |
| Agent behaviour — tool selection / argument accuracy / tool error rate | 🟡 partial | from `messages[].tool_calls`; no separate tool span timing |
| Agent behaviour — handoff fidelity | 🟡 inferred | role-change heuristic between calls |
| Agent behaviour — task success, step efficiency | 🟡 / ❌ | task success yes; step efficiency needs in-process |
| Trace quality — context propagation, dead span rate | ❌ | in-process only (Layer 3) |

Estimated ~55 of 68 metrics fully functional from gateway-only capture; the rest need the optional in-process SDK.

---

## 7. Backward compatibility

- **`opt-demo`:** in-process trigger fires as today. Gateway rows for the same `trace_id` are detected as already-traced → MERGE metadata only, no double eval. Prompt Analysis unchanged.
- **Existing in-process integrations:** untouched. `run_from_targets` is additive; `_run_inner` still calls it via the assembler.
- **`ShadowEvalPipeline`:** either left alone or folded into `GatewayIngestPipeline` with a `shadow` flag. `gateway_shadow_evals` table and its portal view are unaffected either way.
- **`gateway_call_log` schema:** all new columns have defaults; old rows and old readers keep working.

---

## 8. Implementation plan

### Phase 1 — M1 ingest backbone (no M3 code changes)
- [ ] `EvalPipeline.run_from_targets()` refactor + unit test parity with `_run_inner`.
- [ ] `GatewayIngestPipeline` + target synthesis (single-row → single `agent.task` + `llm_call`).
- [ ] `Repository.get_pending_gateway_calls()` / `mark_gateway_calls()` + `gateway_call_eval_state` DDL.
- [ ] `_gateway_ingest_loop()` in `main.py`.
- [ ] Verify: point a raw `openai` client (chat completions) at the gateway → Prompt Analysis + Eval Measurements populate.

### Phase 2 — multi-agent grouping
- [ ] `conversation_id` column + header read in M3.
- [ ] Grouping by trace/conversation/run; multi-row synthesis; handoff inference.
- [ ] `messages_json` column + tool-span extraction.
- [ ] MERGE mode + `trace_has_inprocess_spans` double-eval guard.

### Phase 3 — trigger config + convention normalisation
- [ ] `ingestion.task_span_names` config in `trigger.py` / `trace_assembler.py`.
- [ ] Map OpenLLMetry (`gen_ai.*`) and OpenInference (`llm.*`) attribute names in `extract_eval_targets`.
- [ ] Fixture: a raw OTel GenAI trace (no ACP SDK) triggers evals.

### Phase 4 — protocol completeness
- [ ] `/v1/responses` (+ streaming, tools).
- [ ] Native `/v1/messages`.
- [ ] Gemini `generateContent` / `streamGenerateContent`.
- [ ] `/v1/embeddings`.
- [ ] Per-protocol normalisation into `GatewayCallRecord`.

### Phase 5 — conformance suite
- [ ] Golden capture + expected-eval fixtures for: raw OpenAI SDK, OpenAI Agents SDK, Claude Agent SDK, Google ADK, LangChain. CI asserts `eval_scores` + `prompt_evals` rows appear for each.

---

## 9. Testing / conformance

- **Unit:** `run_from_targets` produces identical `eval_scores` / `prompt_evals` for a synthesised single-call target vs. the equivalent in-process span tree.
- **Integration:** docker-compose up M1 + M3 + ClickHouse; drive traffic with each framework's minimal example; assert portal tables populate within one poll interval + judge latency.
- **Regression:** run `opt-demo` end-to-end; diff Prompt Analysis rows against a pre-change baseline — expect no change (MERGE mode).
- **Load:** 1k calls/min into `gateway_call_log`; confirm the ingest loop keeps up at `batch_size=50` / 5 s and lag stays bounded.

---

## 10. Open questions

1. **Push vs. pull transport.** Pull (poll `gateway_call_log`) needs zero M3 changes and is durable. A push fast-path (`POST /v1/gateway-calls`) cuts latency but adds a failure mode. Recommendation: ship pull; add push later only if latency matters.
2. **Sampling.** In-process evals sample LLM judges at 15%. Should gateway-ingested calls use the same rate, a separate rate, or 100% for low-volume systems? Lean: reuse `gov_threshold_config` `sampling.online_llm_judge_rate`, add a `sampling.gateway_ingest_rate` override.
3. **`eval_trace_id` for un-propagated calls.** Use `call_id` as the trace id, or synthesise `sha256(conversation_id + day)`? Affects how Prompt Analysis groups rows.
4. **Retention.** `messages_json` at 32 KB × high volume grows `gateway_call_log` fast. TTL policy? Separate `gateway_call_payloads` table with shorter TTL?
5. **Streaming timing.** `_stream_and_log` records `latency_ms` for the full stream; is first-token latency also wanted as a separate field?
6. **Does M3 keep emitting `gateway.llm_call` to Jaeger?** Useful for ops trace views; just must not be on the eval trigger path.

---

## 11. Appendix — file-by-file change list

### M3 — `modules/m3-agent-gateway/agent_gateway/`
| File | Change |
|---|---|
| `db.py` | `gateway_call_log` DDL + `log_call`: add `conversation_id`, `protocol`, `started_at`, `ended_at`, `messages_json`, `response_tool_calls_json` |
| `main.py` | read `X-Gateway-Conversation-Id`; new route handlers (Phase 4): `/v1/responses`, `/v1/messages`, `/v1beta/.../:generateContent`, `/v1/embeddings` |
| `proxy.py` | populate new record fields; per-protocol request/response normalisation (Phase 4); factor `GatewayCallRecord` builder out of `handle()` |
| `telemetry.py` | demote or delete `emit_call_span` (no longer an eval input) |

### M1 — `modules/m1-observability-eval/eval_runner/`
| File | Change |
|---|---|
| `pipeline/eval_pipeline.py` | extract `run_from_targets()`; `_run_inner` delegates to it |
| `pipeline/gateway_ingest_pipeline.py` | **new** — batch reader, grouping, target synthesis, MERGE guard |
| `pipeline/trigger.py` | configurable `task_span_names` |
| `ingestion/trace_assembler.py` | `task_span_names` set; OpenLLMetry / OpenInference attribute aliases in `extract_eval_targets` |
| `db/repository.py` | `get_pending_gateway_calls`, `mark_gateway_calls`, `trace_has_inprocess_spans` |
| `main.py` | instantiate `GatewayIngestPipeline`; add `_gateway_ingest_loop` |
| `config/evaluator_config.yaml` | `ingestion.task_span_names`, `sampling.gateway_ingest_rate` |

### Infra — `infra/clickhouse/`
| File | Change |
|---|---|
| `init.sql` | `gateway_call_eval_state` table; new `gateway_call_log` columns |

### Docs (after acceptance)
| File | Change |
|---|---|
| `docs/instrumentation-guide.md` | "gateway-only" becomes the true default path; drop the in-process SDK requirement for basic evals |
| `docs/ai-agent-task-spec.md` | collapse to: set base URL + 3 headers; per-framework protocol notes |
| new `docs/span-contract.md` | the versioned `GatewayCallRecord` + span-name/attribute contract |
