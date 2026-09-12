# Design: Checkpoint / Handoff / Tool-Span Ingest — One Front Door, Not Two

**Status:** Implemented and validated in v4 — see [`v4-implementation-status.md`](./v4-implementation-status.md) for what was actually built, tested against a live stack, and fixed. This doc remains accurate as the architecture rationale; treat the status doc as authoritative for current state.
**Date:** 2026-09-06
**Author:** Raj Ramanujam
**Scope:** Local design work.
**Builds on:** [`design/v2-gateway-capture-m1-ingest.md`](./v2-gateway-capture-m1-ingest.md) (gateway-primary ingest for LLM calls). This doc extends that same architectural direction to the non-LLM-call signals that gateway capture structurally cannot see.

---

## 1. Problem statement

`v2-gateway-capture-m1-ingest.md` establishes that gateway capture (M3) should be the primary, default source of "an LLM call happened" facts for M1 eval and M2 automatic enforcement — because wire-level capture is a fixed, finite protocol surface, while passive in-process instrumentation is an unbounded, per-framework-fragile one (see `docs/external-agent-integration-findings.md`, Issues 1/2/5, all caused by fighting framework internals rather than a gap in the wire protocol).

That leaves an honest, irreducible remainder: signals that never cross the wire to an LLM provider at all —

- **Non-LLM application logic** — a deterministic tool execution, an internal retry loop, a state transition that never calls an LLM.
- **Sub-agent structure** — a handoff from one agent role to another, which is a fact about the *caller's* orchestration, not about any single LLM call.
- **Pre-action gates** — "pause and get human/policy approval before sending this email / executing this trade" — a business-logic checkpoint, not an LLM call.

Today's answer to this remainder is the `acp-tracing` / `acp-governance` SDKs, used as **passive, global instrumentation**: a bootstrap patches the `openai` client, registers a global OTel `TracerProvider`, and expects framework-emitted spans to show up in a specific shape. This is exactly the pattern that produced Issues 1, 2, and 5 in the findings doc — it has to reach into framework internals (module attributes vs. env vars, competing tracing systems, one global role header) that were never designed as an extension point and that change across framework versions.

**This design's claim:** the remainder doesn't need passive instrumentation at all. It needs a small number of *explicit* calls, made through the *same front door* as LLM traffic (the gateway), using each framework's *own documented extension point* (not its internals) as the trigger. That combination is both smaller in surface area and far less fragile than what exists today.

---

## 2. Goals / non-goals

### Goals

1. **One ingress boundary for the entire control plane**, not two. Structural and governance signals that don't fit the LLM-call wire protocol get their own small, explicit endpoint family on the gateway — not a separate SDK-only side channel with no shared trust/logging boundary.
2. **Replace passive interception with explicit, versioned client calls.** No global tracer, no client monkey-patching, no competing with a framework's own telemetry.
3. **Use each framework's official hook/callback/plugin system as the adapter trigger** — never reach into private internals or assume undocumented behavior.
4. **Be honest about what stays manual.** Pre-action gating requires a human decision about which actions are "irreversible enough to need a gate" — no architecture automates that judgment call.
5. **Feed the same M1 ingest pipeline** (`GatewayIngestPipeline`, per the prior design doc) so handoffs and tool-spans become inputs to target synthesis rather than a second reconciliation problem.

### Non-goals

- Reconstructing a fully faithful internal call tree (true nested reasoning depth, dead-span rate) purely from these three signal types. They give *enough* structure for handoff fidelity, tool accuracy, and checkpoint audit — not a full replacement for deep in-process tracing where someone genuinely wants that level of depth.
- Making pre-action gating automatic. That decision is inherently the developer's; this design only makes the *mechanism* for expressing it small and stable.
- Changing anything about the existing LLM-call proxy path (`/v1/chat/completions`, `/v1/responses`, `/v1/messages`, etc.) — this is additive.

---

## 3. Architectural stance

> The gateway is the single ingress boundary for the control plane. LLM-call proxying is one category of traffic through it. Structural signals (handoffs, non-LLM tool calls) and governance checkpoints (pre-action gates) are a second, explicit category through the same door.

Concretely: three new endpoints, same auth model (virtual `gw-sk-*` keys), same durable logging family (`gateway_call_log`-adjacent tables), distinguished by a `call_type` field instead of living in an entirely separate system. An operator (or EvalGov) looking for "everything this control plane has seen" checks one place, not two.

The SDK's job shrinks accordingly — from "a tracing library that must correctly reconstruct call shape across arbitrary framework internals" to "a thin client for three endpoints, plus a handful of maintained per-framework adapters that translate a framework's *own* hook events into those three calls."

---

## 4. New endpoints

All three are synchronous HTTP calls from the agent process to the gateway. All three write to a durable table and are visible in the portal Call Log alongside LLM calls (filterable by `call_type`).

### 4.1 `POST /v1/checkpoint`

Pre-action governance gate. Blocks until a decision is returned (mirrors today's synchronous `governance-service` gate check, just fronted by the gateway instead of called directly).

```jsonc
{
  "schema_version": "1.0",
  "action": "send_email | execute_trade | delete_record | ...",  // free-text, app-defined
  "risk_level": "low | medium | high | critical",                // app's own classification
  "system_id": "string",
  "agent_role": "string",
  "conversation_id": "string",
  "metadata": { /* arbitrary app-supplied context for the reviewer / policy engine */ }
}
```

Response:

```jsonc
{
  "decision": "allow | block | hitl_pending",
  "checkpoint_id": "uuid",
  "reason": "string"        // populated on block or hitl_pending
}
```

Behavior: gateway forwards synchronously to `governance-service` (M2) — no new decision logic, just a shared front door. If `hitl_pending`, the caller is expected to poll or long-poll on `checkpoint_id` the same way HITL is already surfaced today.

### 4.2 `POST /v1/handoff`

Marks a sub-agent transition. Fire-and-forget (does not block the caller).

```jsonc
{
  "schema_version": "1.0",
  "from_agent": "string",
  "to_agent": "string",
  "conversation_id": "string",
  "run_id": "string | ''",
  "context_summary": "string | ''"   // optional — what was handed off, for handoff_fidelity scoring
}
```

### 4.3 `POST /v1/tool-span`

Marks a non-LLM tool execution. Fire-and-forget.

```jsonc
{
  "schema_version": "1.0",
  "tool_name": "string",
  "input": "string | object",
  "output": "string | object",
  "status": "ok | error",
  "latency_ms": 0,
  "agent_role": "string",
  "conversation_id": "string",
  "run_id": "string | ''"
}
```

### 4.4 Storage

New table, sibling to `gateway_call_log` (per the prior design doc's pattern of extending that family rather than inventing a parallel system):

```sql
CREATE TABLE otel.gateway_structural_events (
    event_id        String,
    call_type       LowCardinality(String),   -- 'checkpoint' | 'handoff' | 'tool_span'
    conversation_id String DEFAULT '',
    run_id          String DEFAULT '',
    system_id       String DEFAULT '',
    agent_role      String DEFAULT '',
    payload_json    String,                    -- the type-specific body above
    created_at      DateTime64(3) DEFAULT now64(3)
) ENGINE = MergeTree() ORDER BY (conversation_id, created_at);
```

`GatewayIngestPipeline` (from the prior design doc) reads this table alongside `gateway_call_log` when grouping by `conversation_id`/`run_id`, and folds `handoff` rows into `handoff_spans` and `tool_span` rows into `tool_spans` in its target-synthesis step — no change to `EvalPipeline.run_from_targets()` itself, since it already expects those target lists as input.

---

## 5. SDK re-scoping

### 5.1 The client (all frameworks)

A single thin module (fits in the existing `acp-governance` / `acp-tracing` packages, or a new minimal `acp-signals` package) with three functions, each a direct POST to the endpoints above:

```python
acp.checkpoint(action, risk_level, metadata=None) -> Decision
acp.handoff(from_agent, to_agent, context_summary="")
acp.tool_span(tool_name, input, output, status="ok", latency_ms=0)
```

No global tracer registration. No client monkey-patching. Correlation (`conversation_id`, `run_id`, `agent_role`) is read from a context variable set once per request (see §5.3), not from a global OTel span context.

### 5.2 Per-framework adapters — hook into the framework's own extension point

Each adapter is a small, versioned, ACP-maintained artifact whose only job is: *listen for this framework's own documented event, call the matching client function.* Not a general tracer — one translation per framework, against a stable public API.

| Framework | Native extension point used | Adapter translates |
|---|---|---|
| OpenAI Agents SDK | `agents.RunHooks` (`on_handoff`, `on_tool_start`/`on_tool_end`) | `on_handoff` → `acp.handoff()`; tool hooks → `acp.tool_span()` |
| Google ADK | Plugin / callback interface | Agent-transition callback → `acp.handoff()`; tool callback → `acp.tool_span()` |
| LangChain / LangGraph | `BaseCallbackHandler` (`on_agent_action`, `on_tool_end`, `on_chain_end`) | Chain/agent boundary → `acp.handoff()`; `on_tool_end` → `acp.tool_span()` |
| CrewAI | Step/task callbacks | Task handoff → `acp.handoff()`; tool callback → `acp.tool_span()` |
| Raw / custom agent loops | none — direct calls | Developer calls `acp.handoff()` / `acp.tool_span()` explicitly at the relevant lines |

Each adapter is small enough to be a single file, reviewed once against the framework's public docs, and re-verified (not re-designed) when a framework version bumps — a fundamentally cheaper maintenance loop than debugging why a passive global patch silently stopped working (which is what Issues 1/2/5 actually were).

### 5.3 Context propagation — explicit, not a global tracer

Bootstrap sets one context variable at the top of a request (the same point today's per-agent role header already gets set, per Issue 2's fix):

```python
acp_context.set(conversation_id=..., run_id=..., system_id=..., agent_role=...)
```

`checkpoint()`, `handoff()`, and `tool_span()` read from this context by default (with optional explicit overrides). This avoids two failure modes from the current design: (a) a global OTel `TracerProvider` that competes with a framework's own tracing (Issue 5), and (b) one static role header shared across all agents (Issue 2) — because the contextvar is set/overridden at whatever granularity the adapter needs (e.g. re-set on each `on_handoff` callback to reflect the new active agent).

---

## 6. What this does not solve

- **Which actions need a checkpoint** is a business decision. `acp.checkpoint()` being a one-line, stable call makes it *cheap* to add at the right points — it doesn't identify those points automatically.
- **Deep call-tree fidelity.** Handoff + tool-span records give a flat, ordered structural log per conversation — sufficient for `handoff_fidelity`, `tool_selection_accuracy`, `tool_argument_accuracy`, `tool_error_rate` — not a full reconstruction of nested reasoning depth (`step_efficiency`, `dead_span_rate` stay in the "needs deep in-process tracing" column, unchanged from the prior design doc's §6 table).
- **Frameworks with no callback/hook system at all.** Falls back to the raw/custom row above — explicit calls at the point of need. Still strictly less invasive than today's global-patch approach, just not zero-code.

---

## 7. Relationship to `v2-gateway-capture-m1-ingest.md`

| Concern | Prior doc | This doc |
|---|---|---|
| LLM call capture | `GatewayCallRecord` via existing proxy endpoints | unchanged |
| M1 ingest pipeline | `GatewayIngestPipeline` reads `gateway_call_log` | same pipeline also reads `gateway_structural_events` |
| Target synthesis | builds `task_spans` / `llm_spans` from call records | `handoff_spans` / `tool_spans` now sourced from this doc's table instead of being inferred purely from `messages[].tool_calls` (§3.4 of the prior doc) or a role-change heuristic |
| M2 checkpoint gating | called directly against `governance-service` | fronted by the gateway's new `/v1/checkpoint`, same decision logic, one less direct-to-service integration path for callers to maintain |
| SDK positioning | "Layer 3" richer in-process tracing, co-equal with gateway capture | SDK's remaining scope narrowed specifically to the three explicit signal types here — this doc doesn't reopen whether full passive in-process tracing should still exist as an opt-in power-user path, it just stops treating it as required for correctness |

---

## 8. Implementation plan

### Phase 1 — Endpoints + storage
- [ ] `gateway_structural_events` DDL (`infra/clickhouse/init.sql`).
- [ ] `POST /v1/handoff`, `POST /v1/tool-span` handlers in `agent_gateway/main.py` — auth via existing virtual-key validation, straight insert, no governance/routing logic.
- [ ] `POST /v1/checkpoint` handler — forwards to `governance-service`, returns its decision, logs to the same table.

### Phase 2 — Thin client + context propagation
- [ ] `acp.checkpoint()` / `acp.handoff()` / `acp.tool_span()` client functions.
- [ ] `acp_context` contextvar module — set/read conversation/run/system/role.

### Phase 3 — Per-framework adapters
- [ ] OpenAI Agents SDK (`RunHooks`) adapter.
- [ ] Google ADK plugin adapter.
- [ ] LangChain/LangGraph callback handler adapter.
- [ ] CrewAI callback adapter.
- [ ] Documented raw/custom pattern for anything else.

### Phase 4 — M1 ingest integration
- [ ] `GatewayIngestPipeline` reads `gateway_structural_events`, folds into `handoff_spans` / `tool_spans` during target synthesis.
- [ ] Regression check: `handoff_fidelity` / `tool_selection_accuracy` / `tool_argument_accuracy` scores computed from this path match (or improve on) the previous `messages[].tool_calls`-inference approach.

### Phase 5 — Conformance
- [ ] Golden fixture per framework adapter: drive a scripted handoff + tool call through each framework, assert the three synthesized target types (task/llm/handoff/tool spans) appear correctly and score.

---

## 9. Open questions

1. **Does `/v1/checkpoint` replace direct `governance-service` calls, or wrap them?** Leaning: gateway forwards, doesn't reimplement — avoids duplicating decision logic, keeps one place (M2) owning policy.
2. **Should `tool_span`/`handoff` calls be batched/async client-side** to avoid adding latency to the agent's own execution path, given they're fire-and-forget? Likely yes — a small local queue flushed async, mirroring how the gateway itself does `_emit_async` for call logging.
3. **Retention for `gateway_structural_events`** — same question as `messages_json` in the prior design doc (§10.4): tool input/output payloads can be large; needs a TTL policy, tracked together with that doc's retention item rather than solved twice.
4. **Does a full passive in-process tracer still deserve to exist as an opt-in "Layer 3" for power users** who want deep call-tree fidelity beyond what these three signal types give? Out of scope here — this doc only asserts it's no longer *required* for baseline correctness, not that it should be removed.
