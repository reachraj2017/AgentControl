# Design: Self-Hosted System 1 Decisions via Strands Decider

**Status:** Draft — not yet implemented
**Date:** 2026-10-11
**Author:** reachraj2017
**Scope:** Local design work.
**Relation to prior work:** Same candidate list and rationale as `design-backlog/jev-system-one-model-integration.md` — this doc exists because Strands Decider resolves that doc's two biggest open caveats (vendor maturity, network latency to an external API) by being open-weight and self-hostable. Read that doc first; this one only covers what's different.

---

## 1. Problem statement

`jev-system-one-model-integration.md` identified several places in this platform where a fast, cheap, typed classification/score decision is a better fit than a full LLM judge call — M1's structured eval metrics, M2's inline enforcement gates, M4's anomaly detection step — but flagged two real caveats against adopting Jev specifically: it's a closed, hosted, single-vendor API with no production track record, and every call leaves this platform's infrastructure.

**Strands Decider** ([github.com/strands-labs/strands-decider](https://github.com/strands-labs/strands-decider)) is Amazon/Strands Labs' open-weight (Apache 2.0) model in the same "System 1 decision" category — explicitly benchmarked against JevBench, same three question types (`noul`=yes/no, `choice`, `score`), same one-forward-pass-no-generation architecture. The difference that matters here: it's a small (2B–26B parameter) decoder model with a lightweight pointer head, published on Hugging Face, that runs on CPU, Apple Silicon, or GPU — meaning it can run as **another container in this platform's own Docker Compose stack**, with no API key, no egress, no per-call billing, and a public, reproducible benchmark (77.9% accuracy on JevBench, Brier score 0.280, ~95% correct at 0.9+ confidence) instead of vendor marketing claims.

It also ships a demonstrated agent-framework integration pattern — a `before_tool_call` hook in the Strands Agents SDK that gates tool execution, having the agent ask a clarifying question rather than guess — which maps directly onto this platform's own `acp-signals` checkpoint mechanism (see §3.3).

Goal of this design: describe where Strands Decider fits as a self-hosted decision backend, how it would be deployed alongside the existing stack, and how it specifically improves on the Jev design's open caveats.

---

## 2. Goals / non-goals

### Goals
1. Reuse the candidate-surface analysis from the Jev design (M1 judges, M2 inline gates, M4 anomaly detection) without re-deriving it — only note where Strands Decider changes the calculus.
2. Design self-hosted deployment as a new Docker Compose service, consistent with this platform's existing all-local, `make up`-driven architecture — no new external dependency, no new secret/API key to manage.
3. Map the model-size tradeoff (2B through 26B) onto this platform's actual latency-sensitivity tiers: inline/synchronous (M2 enforcement) vs. offline/batch (M1 judging, M4 monitor).
4. Connect the `before_tool_call` pattern concretely to the existing `acp-signals.checkpoint()` call path, identifying exactly where a local decider could sit in that flow.
5. Validate via the same shadow/A-B evidence mechanism proposed in the Jev design — public JevBench numbers are a starting point, not a substitute for measuring against this platform's own traffic.

### Non-goals
- Re-litigating which metrics/gates are good System-1 candidates — see `jev-system-one-model-integration.md` §3 for that analysis; it applies unchanged.
- Fine-tuning or retraining Strands Decider on this platform's own data. This design only covers using a published model as-is.
- Replacing `acp-signals.checkpoint()`'s governance-service policy engine. A local decider is a candidate *input* to a checkpoint decision, not a replacement for the governance service's existing policy logic.

---

## 3. Proposed design

### 3.1 Deployment — a new local service, not an external call

Add a `strands-decider` service to `docker-compose.yml`, served via `strands-decider serve <model> --port 8000`-equivalent, running one of the published models (start with the 2B Qwen3.5 reference model, `strands-decider-2B-qwen3.5-v1-2610`, available from both the `StrandsAgents` and `amazon` Hugging Face orgs). This is a CPU-servable model, so no GPU requirement is added to the stack's existing prerequisites (`docker-compose.yml`'s current services are all CPU-based already).

This directly removes the Jev design's "network overhead to an external API" open question (§7.2 of that doc) — latency becomes container-to-container on the same Docker network, not a round trip to `thejevai.com`.

### 3.2 M1 — eval judges, same candidates as the Jev design

Same analysis as `jev-system-one-model-integration.md` §3.1: bounded metrics (PII detection, faithfulness, relevance, tool-selection/argument accuracy, hallucination, bias, toxicity) are candidates for a decider-backed evaluator, implemented the same way — a new class under `evaluators/llm_judges/`, run as a shadow-only evaluator initially, validated against the incumbent LLM judge via `compare_shadow_vs_primary` before any traffic shifts.

One correction worth noting: `ToolSelectionEvaluator`/`ToolArgumentAccuracyEvaluator` (`modules/m1-observability-eval/eval_runner/evaluators/deterministic/`) are currently **deterministic** evaluators, not LLM judges — they compare against an expected tool/argument set directly. A decider model isn't a replacement for those; it's a candidate for the metrics that are genuinely LLM-judged today (faithfulness, relevance, PII, hallucination, bias/toxicity, instruction-following).

### 3.3 M2 — the `before_tool_call` pattern maps onto `acp-signals.checkpoint()`

This is the most concrete new angle Strands Decider adds. `acp_signals/client.py`'s `checkpoint()` (line 83) is a **synchronous, blocking pre-action gate**: an agent calls it before a risky action, it POSTs to `/v1/checkpoint`, the gateway forwards to the M2 governance service, and the call blocks until a decision comes back — failing open on error or timeout (30s default) per the client's documented fail-open posture.

Two places a local decider could sit in that flow, in increasing order of change:

1. **Client-side pre-filter, before the network round trip.** For a clearly-safe or clearly-unsafe action, a local decider call (same container-to-container latency as §3.1, no network hop to the gateway at all) could resolve the decision immediately, reserving the full `checkpoint()` round trip (and whatever governance policy logic it runs) for genuinely ambiguous cases. This reduces checkpoint latency/volume without changing the governance service's decision authority — the local decider only ever short-circuits the *obvious* cases, deferring anything uncertain to the real path.
2. **Input to the governance service's own policy decision.** The governance service could call the local decider itself as one signal among others when a checkpoint request doesn't match a hard rule — e.g., using a `score`-type question ("how risky is this action, 0–100") as a factor alongside existing policy checks, rather than a hard override.

Both are additive — the governance service's policy engine remains the decision authority; a decider is a fast pre-check or an additional signal, never a silent override. This is consistent with `jev-system-one-model-integration.md`'s §5 caveat about blast radius: a fast, wrong signal is still wrong, so neither integration point should bypass the fail-open/fail-closed posture `checkpoint()` already documents.

### 3.4 M4 — anomaly detection, same fit as the Jev design

Unchanged from `jev-system-one-model-integration.md` §3.3 — the monitor's detection step is a bounded decision; its root-cause narrative generation is not, and stays on the current LLM.

### 3.5 Model-size selection by latency tier

Strands Decider's size range (2B/E2B/E4B through 12B/26B-A4B) gives a tradeoff the single-model Jev doesn't offer:

| Tier | Latency budget | Suggested size | Use |
|---|---|---|---|
| Inline/synchronous | Sub-200ms (matches published 115ms/153ms medians) | 2B (Qwen3.5 reference) | §3.3's client-side pre-filter, any check in the gateway's synchronous request path |
| Offline/batch | Seconds acceptable | 12B or 26B-A4B | M1 shadow judge evaluation, M4 monitor's periodic poll — accuracy/calibration gains matter more than latency here |

This mirrors the tiering already proposed for autonomy decisions in `design-backlog/autonomous-self-improvement-loop.md` — different risk/latency tiers get different mechanisms, not one-size-fits-all.

### 3.6 Not a fit

Unchanged from the Jev design: EvalGov conversational answers, prompt-mod authoring, root-cause narrative text. Generation stays on a frontier LLM.

---

## 4. Validation approach

Same mechanism proposed in `jev-system-one-model-integration.md` §4 — shadow the decider against the incumbent LLM judge via `compare_shadow_vs_primary`, measure agreement rate and score-delta distribution on this platform's own traffic, and only propose a change through the existing `/gateway/changes` queue once that evidence clears a bar. The published JevBench numbers (77.9% accuracy, Brier 0.280) are a reasonable prior to decide *which* metrics are worth trialing first — favor ones closer to the benchmark's own task mix (classification/triage-shaped, like PII detection and routing-style decisions) over ones that need deeper context (multi-turn metrics) as the first trial.

---

## 5. Risk / caveats

- **Self-hosted ≠ zero-maintenance.** Running an additional model service means this platform now owns a new container's resource footprint (RAM for a 2B–26B model) and update cadence — a real operational cost the hosted-API alternative doesn't have, traded for removing the vendor/egress risk.
- **Smaller than frontier, by design.** 77.9% accuracy on JevBench is good for a sub-200ms decision, not comparable to a frontier LLM judge's accuracy on hard cases — the shadow-validation step in §4 exists specifically to find where that gap matters for this platform's actual metrics, not to assume parity.
- **Still new.** Strands Decider is a recent release; treat the same way as Jev — pluggable, optional, validated with this platform's own evidence before being trusted for anything in Tier 2+ of the autonomy design.

---

## 6. Implementation plan (draft — not started)

### Phase 1 — Deploy the service
- [ ] Add `strands-decider` to `docker-compose.yml`, serving the 2B Qwen3.5 reference model.
- [ ] Confirm container-to-container reachability from both the eval runner (M1) and governance service (M2).

### Phase 2 — Shadow evaluator, M1
- [ ] Same as Jev design Phase 1: new evaluator class, shadow-only, starting with PII detection and one score-type metric.
- [ ] Compare against incumbent LLM judge via `compare_shadow_vs_primary`.

### Phase 3 — Checkpoint pre-filter pilot, M2
- [ ] Client-side pre-filter in `acp-signals` (§3.3 option 1) for one well-understood action type, with full `checkpoint()` still running in parallel (shadow, not replacing) to measure agreement before trusting the short-circuit.

### Phase 4 — Docs
- [ ] If adopted, document the new service and its role in `docs/operator-runbook.md`, alongside the existing `JUDGE_MODEL` documentation pattern.

---

## 7. Open questions

1. Does the 2B model's accuracy hold up on this platform's actual action/metric distribution, which may differ meaningfully from JevBench's 231-task mix? Only answerable via §4's shadow comparison.
2. Is the client-side pre-filter (§3.3 option 1) worth the added complexity in `acp-signals`, or should the first iteration just put a decider behind the M1 shadow-evaluator case (§3.2), which needs no SDK changes at all? Leaning toward M1 first — it's the lower-risk, higher-evidence-value starting point, same ordering as the Jev design's phase plan.
3. Should this platform standardize on one System-1 backend (Jev or Strands Decider) or keep both pluggable as alternative evaluator classes and let the shadow-comparison evidence decide per-metric? Leaning toward keeping both pluggable — the evaluator interface proposed in both docs is identical, so there's little cost to trialing both and letting evidence choose.
