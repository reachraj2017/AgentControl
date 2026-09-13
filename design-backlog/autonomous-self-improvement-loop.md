# Design: Autonomous Self-Improvement Loop (Trigger → Tiered Autonomy → Verify/Rollback)

**Status:** Draft — not yet implemented
**Date:** 2026-09-13
**Author:** reachraj2017
**Scope:** Local design work.

---

## 1. Problem statement

The control plane already closes an evidence → proposal → apply → re-measure loop (see README's "The self-improving loop"), but every step today depends on a human:

- **Trigger:** a proposal only gets created when someone (an operator, or the gateway/EvalGov agent mid-conversation) explicitly calls `propose_gateway_change`. The proactive monitor (M4) already runs unattended every `MONITOR_POLL_SECONDS` and writes findings, but findings are informational — nothing turns a finding into a proposal on its own.
- **Apply:** every change, regardless of type or risk, sits in `Proposed Changes` (`portal/pages/36_Changes.py`) until an operator clicks approve. `PUT /gateway/changes/{id}` (`modules/m3-agent-gateway/agent_gateway/operations.py:555`) calls `_db.auto_apply_change(change_id)` only after that human decision.
- **Verify/rollback:** once applied, nothing watches the change afterward. If a routing policy or prompt mod regresses quality, the only way it gets caught is an operator noticing a drop in Eval Measurements or a new proactive-monitor finding — there is no automatic bake window or revert.

Goal of this design: describe what it would take to close each of those three gaps, while being explicit about which changes are safe to fully automate and which must stay human-gated regardless of how much evidence accumulates.

---

## 2. Goals / non-goals

### Goals
1. Let a completed shadow comparison or A/B test result automatically produce a proposal, without a human or chat message initiating it.
2. Let a subset of low-blast-radius change types auto-apply without waiting in the approval queue, while everything else keeps today's human gate.
3. Add a bake/verify window after any applied change (autonomous or human-approved) that watches eval scores and can auto-revert on regression.
4. Make every autonomous action distinguishable from a human-initiated one in the audit trail — an operator must always be able to tell "the loop did this" from "I did this."
5. A single kill switch that disables all autonomous behavior instantly, independent of the per-change-type tiering.

### Non-goals
- Autonomous action on anything outside the gateway's existing change types (`routing_policy`, `shadow_rule`, `prompt_mod`). Governance actions (`quarantine_agent`, `reset_circuit_breaker`, key revocation) are explicitly out of scope for auto-apply in this design — they stay human-only regardless of tier (see §4).
- Changing how evidence itself is generated (shadow mode, A/B tests, the 68-metric pipeline) — this design only adds a scheduling/policy/verification layer on top of what already produces evidence.
- A general-purpose workflow/rules engine. The tiering policy described here is intentionally a small, explicit table, not a new DSL.

---

## 3. Proposed design

### 3.1 Trigger — from "chat-initiated" to "monitor-initiated"

The proactive monitor (`modules/m4-intelligence`, see `README.md`'s "Proactive monitor" description) already runs a fixed set of checks on a poll loop and writes findings with an LLM-generated root cause analysis. Add one more check to that same loop:

- **New check: "proposal-worthy evidence."** On each poll, query `compare_shadow_vs_primary` (already used by the gateway agent, `modules/m3-agent-gateway/gateway_agent/tools.py:330`) and completed A/B tests (`get_ab_test_results`) for results that cross a minimum evidence bar (see §3.2's thresholds). When one does, call `propose_gateway_change` directly from the monitor loop instead of waiting for an operator or chat turn to do it.
- This reuses the exact same proposal path that exists today (`POST /gateway/changes`) — the only change is *who* calls it. `proposed_by` should be set to a distinct value (e.g. `"proactive-monitor"`) rather than `"gateway-agent"` or `"operator"`, so the source is visible in the Changes queue and its history tabs without any schema change (the column already exists — see `36_Changes.py`'s `by = ch.get("approved_by") or ch.get("proposed_by", "?")`).

### 3.2 Tiered autonomy — a small, explicit risk table

Add a policy check between "a change is proposed" and "a change is applied," keyed on `change_type`:

| Tier | Change types | Auto-apply condition | Rationale |
|---|---|---|---|
| **1 — no production impact** | `shadow_rule` create/disable, `ab_test` create | Always auto-apply | Shadow traffic and A/B variants don't change what production callers receive by definition — a bad shadow rule wastes compute, not correctness. |
| **2 — live traffic, reversible** | `routing_policy`, `prompt_mod` | Auto-apply only if evidence meets a minimum bar: sample size ≥ N calls (e.g. 200), eval-score delta ≥ a configured margin (not just directionally better — noise-resistant), and cost/latency within a configured bound | These change what live traffic sees, but both are single-row toggles that the same apply/rollback mechanism (§3.3) can revert in one step. |
| **3 — always human-gated** | key revocation/creation, traffic pool/policy deletion, anything from the governance agent (`quarantine_agent`, `reset_circuit_breaker`, HITL decisions) | Never auto-apply, regardless of evidence | Either hard to reverse cleanly (revoking a key breaks any caller still using it) or carries a policy/compliance judgment call that evidence alone doesn't resolve. |

Implementation shape: a small config table or static mapping (e.g. `autonomy_policy` in the gateway service, next to where `change_type` is already validated in `operations.py`), checked inside `decide_change`'s auto-apply branch — a Tier 1/2-qualifying proposal transitions straight to `approved`/`applied` with `approved_by = "autonomous-loop"` instead of waiting for a `PUT` from the portal. A global `AUTONOMY_ENABLED` flag (env var, default `false`) gates the whole mechanism — with it off, every proposal behaves exactly as it does today regardless of tier.

### 3.3 Verify / bake window — closing the loop the other direction

After any change transitions to `applied` — autonomous or human-approved — start a bake window instead of considering the change finished:

1. Record the pre-change baseline (the eval scores for the affected `agent_role`/`system_id` over the window immediately before apply — the same query `compare_shadow_vs_primary` already runs, just against the old vs. new config).
2. For a configured bake period (e.g. N minutes or M subsequent calls, whichever first), keep watching the same M1 eval scores for calls under the new config.
3. If scores regress past a guard threshold during the bake window: automatically propose and auto-apply a **reverting change** (re-disable the routing policy/prompt mod, restoring the prior config), tagged `proposed_by = "autonomous-loop"`, `change_type` unchanged but `description` noting it's a rollback of change `{id}`. Raise a finding through the same proactive-monitor findings mechanism so it's visible in the portal and surfaced at the start of the next EvalGov conversation.
4. If scores hold through the bake window, mark the change `confirmed` (new status value, alongside existing `pending`/`approved`/`rejected`) — purely informational, so the Changes history view can distinguish "applied and held" from "applied, still baking."

This reuses the same change-queue table and apply mechanism from §3.1/3.2 — a rollback is just another change going through the identical propose → (auto-)apply path, not a new code path.

### 3.4 Audit trail

Two additions make autonomous vs. human action unambiguous without new tables:
- `proposed_by` / `approved_by` get one new possible value each: `"proactive-monitor"` (trigger) and `"autonomous-loop"` (tiered auto-apply and auto-rollback), distinct from existing values like `"operator"` or an agent role name.
- `36_Changes.py`'s history tabs (`_render_history`, already rendering `by = ch.get("approved_by") or ch.get("proposed_by", "?")`) need no schema change to show this — just a visual badge (e.g. a 🤖 marker) when `by` is one of the two autonomous values, so an operator scanning the Approved/Rejected/All tabs can immediately tell which rows they never touched.

---

## 4. What stays human-only, and why

Explicitly out of auto-apply regardless of how strong the evidence gets:
- Anything the **governance agent** can do (`quarantine_agent`, `reset_circuit_breaker`, HITL approve/reject, incident resolution) — these are judgment calls about a specific agent's behavior/trust, not a routing/prompt A-B comparison with a clean evidence metric.
- Gateway key creation/revocation and traffic pool/policy deletion — hard to reverse cleanly (a revoked key breaks any in-flight caller immediately; deleting a pool that's serving traffic has no clean auto-revert).
- Changing the tiering policy itself, or the `AUTONOMY_ENABLED` flag — the loop should never be able to widen its own authority.

---

## 5. Data model additions

- No new tables required. Existing `gateway_changes` (or equivalent — the table backing `/gateway/changes`) already has `proposed_by`, `approved_by`, `status`, `change_type`, `payload`, `evidence` columns; this design adds two new *values* for the by-columns and one new status value (`confirmed`), not new columns.
- New: an `autonomy_policy` table or static config (change_type → tier, thresholds) — small, likely under 10 rows, could reasonably start as a config file rather than a DB table.
- New: an `AUTONOMY_ENABLED` env var (default `false`), following the same pattern as `GATEWAY_AUTH_ENABLED` and `MONITOR_POLL_SECONDS` in the README's environment variables table.

---

## 6. Implementation plan (draft — not started)

### Phase 1 — Trigger
- [ ] Add "proposal-worthy evidence" check to the M4 proactive monitor's poll loop.
- [ ] Monitor calls `propose_gateway_change` (or the underlying `POST /gateway/changes`) directly when a shadow/A-B result crosses the evidence bar, tagged `proposed_by = "proactive-monitor"`.

### Phase 2 — Tiered autonomy
- [ ] Define the `autonomy_policy` config (tier per `change_type`, thresholds for Tier 2).
- [ ] `AUTONOMY_ENABLED` env var, default off.
- [ ] `decide_change` / `auto_apply_change` path checks the policy and, when eligible, applies immediately with `approved_by = "autonomous-loop"` instead of waiting on a portal `PUT`.
- [ ] `36_Changes.py` badge for autonomous rows.

### Phase 3 — Verify / rollback
- [ ] Baseline capture at apply time (pre-change eval scores for the affected scope).
- [ ] Background bake-window watcher (could live alongside the proactive monitor, or as its own poll loop in the gateway agent) comparing post-apply scores against the baseline and the configured guard threshold.
- [ ] Auto-revert path: propose + auto-apply a reverting change on regression, with a finding raised through the existing findings mechanism.
- [ ] `confirmed` status once a change survives its bake window.

### Phase 4 — Docs
- [ ] README: fold this into "The self-improving loop" once implemented (today's section should stay accurate to the human-gated version until then).
- [ ] `docs/operator-runbook.md`: how to read the 🤖 badge, how to disable `AUTONOMY_ENABLED`, what a bake-window rollback finding looks like.

---

## 7. Open questions

1. Where should the bake-window watcher actually live — a new background task in the gateway agent (M3), or an extension of the M4 proactive monitor's existing poll loop? Leaning toward M4, since it already owns "watch things over time and raise findings," and the gateway agent is currently stateless/on-demand (per `modules/m4-intelligence/README.md`'s "sub-agents are stateless HTTP services" description) — adding a stateful watcher there would be a bigger structural change than extending the one component that's already a background loop.
2. Should Tier 2's evidence thresholds (sample size, score delta, cost/latency bounds) be global defaults or configurable per `agent_role`/`system_id`? Leaning toward global defaults with per-scope override as a later refinement — start simple.
3. Is a single bake window sufficient, or does a routing/prompt change need a staged rollout (e.g. apply to 10% of matching traffic, then 100%) before this design's rollback mechanism is meaningful? This design assumes the existing A/B infrastructure already provides that staging *before* a change is proposed (i.e., by the time something is proposed, it already won an A/B test against a traffic split) — the bake window here is a second, post-apply safety net, not the primary evidence source.
