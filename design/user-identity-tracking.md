# Design: End-User Identity Tracking (Call Log + Prompt Analysis)

**Status:** Draft — not yet implemented
**Date:** 2026-09-10
**Author:** reachraj2017
**Scope:** Local design work.

---

## 1. Problem statement

Every identifier that currently flows through the control plane answers a different question than "which human is this":

| Identifier | Answers |
|---|---|
| `system_id` | Which product/agent system made this call |
| `agent_role` | Which agent/sub-agent inside that system |
| `conversation_id` | Which session/conversation this call belongs to |

None of these is stable across multiple sessions for the same person. A "user" is a human who may have many `conversation_id`s over time — a dimension orthogonal to all three that exist today.

**This concept does not exist anywhere in the pipeline.** Not in `gateway_call_log`'s schema, not on any OTel span attribute, not in `prompt_evals`. Notably, `opt-demo/runner.py` already threads a `user_id` parameter through its own code (it keys ADK's local session store, `_sessions: dict[str, str]`), but that identifier is pure local bookkeeping — it never reaches the gateway, the tracer, or ClickHouse.

Goal: let an integrator attribute calls/conversations to an end user, and see that as a column in both the portal's Call Log (M3) and Prompt Analysis (M1) — mirroring how `conversation_id` already works end to end.

---

## 2. Goals / non-goals

### Goals
1. A `user_id`-equivalent identifier visible as a column in both Call Log and Prompt Analysis, for both gateway-only and in-process-traced integrations.
2. Optional, not mandatory — many integrations (batch/backend systems) have no natural "user" concept; absence must degrade gracefully, same as `conversation_id` does today.
3. Standards-aligned where it doesn't cost anything, following this system's existing standards-first philosophy for Layer 2.
4. Privacy-conscious by design, not as an afterthought — this is the first identifier in the system that is routinely PII.

### Non-goals
- User-level authentication/authorization (this is an *attribution* label, not an identity/access system).
- Retroactively backfilling historical calls with a user identifier — this only applies going forward.
- Solving the broader `messages_json`/payload retention question (a separate, already-open item — no TTL policy currently exists for stored message/payload content) — this design should not make that problem worse (see §5, hashing recommendation) but does not solve it either.

---

## 3. Proposed design — reusing `conversation_id`'s exact propagation pattern

`conversation_id` already solves the identical structural problem (a cross-cutting identity that must reach both the gateway and the eval pipeline). This design is deliberately not a new pattern — it's the same one, one layer over.

### 3.1 Layer 1 — Gateway wire capture
- New header: `X-Gateway-User-Id`, read in `agent_gateway/main.py` the same place `X-Gateway-Conversation-Id` is read today (`_gw_context`).
- New column `user_id String DEFAULT ''` on `gateway_call_log` (DDL in `db.py` + `log_call`'s insert), following the same additive-column pattern already used for `conversation_id`/`protocol`/etc.
- Automatically appears in `/gateway/calls`'s response (no endpoint change needed — it's a plain column) and can be added to the portal's Call Log table exactly as the `prompt`/`response` columns were just added.

### 3.2 Layer 2 — Standards-based / ACP-native tracing
- For in-process tracing to carry this into Prompt Analysis, it needs to be a span attribute on the `agent.task` span — the same way `runner.py` already does `root_span.set_attribute("conversation.id", conversation_id)`.
- Requires threading a new parameter through `run_agent()` → `_run_async()` → `_call_agent()` in `opt-demo/runner.py` (or the equivalent call chain in any other in-process-traced integration), exactly parallel to how `conversation_id` is threaded today.
- **Naming choice (see §4):** span attribute key should be `enduser.id`, not a proprietary `user.id` — this is a real, existing general OTel semantic convention (not GenAI-specific), so any third-party framework that already tags end-user identity in its own OTel instrumentation shows up correctly with zero extra code.

### 3.3 Layer 3 — Explicit signals (`acp-signals`), for consistency
- `acp_signals.context.set(...)` already carries `conversation_id`/`system_id`/`agent_role`. Add `user_id` there too so `checkpoint()`/`handoff()`/`tool_span()` calls carry it consistently, even though it isn't directly displayed in either target table today — keeps the identity model uniform across all three layers rather than leaving Layer 3 as a gap.

### 3.4 Gateway-only integrations (no in-process tracer)
- `GatewayIngestPipeline`'s SYNTHESISE path (`_synthesise_targets` in `gateway_ingest_pipeline.py`) must carry `user_id` from the gateway row into the synthesized task span's attributes, the same way it already carries `conversation_id`/`agent_role` — otherwise gateway-only callers would only get this in Call Log, not Prompt Analysis, breaking the "gateway-only gets full eval coverage" promise for this one field.

### 3.5 M1 ingestion + storage
- New column `user_id String DEFAULT ''` on `otel.prompt_evals` (and, for consistency, consider whether `otel.eval_scores` needs it too — likely not, since scores are keyed by `trace_id`/`span_id` and can join back to `prompt_evals` for user attribution rather than duplicating the column).
- `trace_assembler.py`'s attribute extraction and `eval_pipeline.py`'s `_save_prompt_eval` need to read `enduser.id` (or the ACP-native equivalent, if `agent.task` spans set it under a different key — decide one canonical key and apply it consistently across native and dialect-recognized spans, the same normalization discipline `semconv_mapping.py` already applies to task/LLM-call recognition).

### 3.6 Portal
- Add `user_id` as a column in Call Log (`portal/pages/31_Call_Log.py`) — same pattern as the `prompt`/`response` columns just added (no truncation needed, this is normally a short string).
- Add the equivalent column in Prompt Analysis (`portal/pages/12_Eval_Measurements.py`).
- Consider a filter control (like the existing System ID / Agent Role filters in Call Log) once the column exists, so an operator can look at one user's activity across calls.

---

## 4. Naming decision

Two reasonable options, deliberately not defaulting to the obvious one without weighing it:

| Layer | Option A (proprietary, consistent with existing ACP naming) | Option B (standards-aligned) |
|---|---|---|
| HTTP header | `X-Gateway-User-Id` | *(same — headers are ACP's own convention regardless)* |
| Span attribute | `user.id` (matches `conversation.id`'s style exactly) | `enduser.id` (real OTel semantic convention) |
| DB column | `user_id` | `user_id` |

**Recommendation:** `X-Gateway-User-Id` at the header/DB level (matches the existing `agent_role`/`system_id`/`conversation_id` convention exactly — no reason to deviate at this layer, since it's ACP's own API surface), but `enduser.id` at the span-attribute level — mirroring the fact that `conversation.id` and `X-Gateway-Conversation-Id` already take slightly different forms at each layer, and gives Layer 2 the standards-alignment benefit for free.

---

## 5. Privacy — a real decision, not an afterthought

A user identifier is often PII (email, username) in a way `system_id`/`agent_role` never are, and it would land in ClickHouse and the portal with no retention policy — compounding the already-open question of retention for stored message/payload content generally.

Options, to be decided before implementation, not during:
1. **Accept whatever the integrator sends, document the risk.** Simplest, but pushes a real compliance decision onto every integrator silently.
2. **Recommend (in `docs/instrumentation-guide.md`) that integrators pass an opaque or hashed ID**, not a raw email/username — e.g. `sha256(real_user_id)` — preserving the ability to correlate a *given* user's activity across calls without ACP ever storing the raw identifier.
3. **Both** — accept whatever is sent (no enforcement), but document the hashing recommendation clearly and prominently, treating it the same way `checkpoint()`'s fail-open governance posture is already documented as a conscious tradeoff rather than a silent default.

Leaning toward option 3, consistent with how this system already treats other governance-adjacent defaults (documented, not silently assumed).

---

## 6. Implementation plan (draft — not started)

### Phase 1 — Gateway + Call Log (Layer 1 only, gateway-only integrations)
- [ ] `X-Gateway-User-Id` header read in `main.py`.
- [ ] `user_id` column on `gateway_call_log` (DDL + `log_call`).
- [ ] `user_id` column added to Call Log's main table (`31_Call_Log.py`), same pattern as the recent `prompt`/`response` addition.
- [ ] `GatewayIngestPipeline` carries `user_id` into synthesized task-span attributes.

### Phase 2 — In-process tracing (Layer 2) + Prompt Analysis
- [ ] Thread a `user_id` parameter through `opt-demo/runner.py`'s call chain, setting `enduser.id` on the `agent.task` span (mirrors `conversation_id`'s existing threading).
- [ ] `trace_assembler.py` / `eval_pipeline.py` extract `enduser.id` into `prompt_evals`.
- [ ] `user_id` column on `otel.prompt_evals`.
- [ ] `user_id` column added to Prompt Analysis (`12_Eval_Measurements.py`).

### Phase 3 — Layer 3 consistency + docs
- [ ] `acp_signals.context` gains `user_id`.
- [ ] `docs/instrumentation-guide.md` gets a short section: how to set the header/context, and the hashing recommendation from §5.
- [ ] Optional: a per-user filter control in Call Log, once the column exists and there's real multi-user data to filter.

---

## 7. Open questions

1. Does `eval_scores` need its own `user_id` column, or is joining back to `prompt_evals` via `trace_id`/`span_id` sufficient? Leaning toward the latter — avoid duplicating an identity column across tables when a join already answers it.
2. Should the portal eventually support a "this user's conversations" cross-module view (spanning Call Log + Prompt Analysis + EvalGov), or is a column on each existing table sufficient for now? Out of scope for this design — a column is the minimum useful unit; a dedicated view is a separate, later decision.
3. Should `checkpoint()` calls (Layer 3, M2) use `user_id` as an input to policy decisions (e.g. a stricter policy for a flagged user)? Not addressed here — this design only covers making the identifier visible, not using it in enforcement logic.
