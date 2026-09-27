# Design: Integrating a System 1 Model (Jev) as a Judge/Enforcement Backend

**Status:** Draft — not yet implemented
**Date:** 2026-09-27
**Author:** reachraj2017
**Scope:** Local design work.

---

## 1. Problem statement

Several parts of the control plane already ask an LLM a *structured* question — a bounded classification, a 0–100 score, a yes/no with confidence — and parse the free-text/JSON answer back into a number or label. This is more expensive and slower than the decision itself requires, and the sampling behavior already in the code is direct evidence of that cost:

- `eval_pipeline.py` only runs LLM judges on a **sample** of calls (`online_llm_judge_rate`, default `0.15`) plus always-on-failure — a workaround for judges being too slow/costly to run on every call.
- `shadow_eval_pipeline.py` points `JUDGE_MODEL` (default `gpt-4o-mini`) at a conversational LLM to answer what are, in every case, bounded scoring questions (faithfulness, relevance, instruction-following).
- M2's real-time enforcement gates (PII, prompt injection, scope compliance, safety) run inline in the gateway's request path, where LLM latency is directly user-facing.

**Jev** (TypeSafe AI's "System 1 Model," see [typesafe.ai/blog/introducing-system-one-models-and-jev](https://typesafe.ai/blog/introducing-system-one-models-and-jev)) is built specifically for this shape of problem: typed schema in, typed probabilistic decision out — no free-text parsing, no generation, claimed 40–200x lower latency (70–500ms) and near-zero output-token cost versus a frontier LLM for equivalent-complexity classification tasks. It supports three question types (choice / score / bool-probability) over text or JSON input, via `POST https://thejevai.com/v1/systemone` (model `jev-latest`).

Goal of this design: identify where in the current architecture a System 1 model is a credible substitute or shadow-candidate for an existing LLM-judge call, and how to validate that claim using the platform's own shadow/A-B infrastructure before trusting it in the request path — without taking a hard dependency on a brand-new, early-access, single-vendor product.

---

## 2. Goals / non-goals

### Goals
1. Identify every existing structured (classification/score/bool) LLM call in the pipeline that is a plausible Jev candidate, distinct from the generation-heavy calls that are not.
2. Design the integration as a pluggable evaluator/judge, not a replacement of the existing LLM-judge path — the platform must work identically with Jev absent.
3. Reuse the existing shadow-mode/A-B/`compare_shadow_vs_primary` machinery (M3) to validate agreement rate and score parity against current judges before any traffic shifts over, rather than trusting vendor benchmarks.
4. Keep the blast radius of a bad Jev response bounded — never let it be the sole decision-maker for anything in Tier 3 of the autonomy design (`design-backlog/autonomous-self-improvement-loop.md`) without the same evidence bar applied to any other change.

### Non-goals
- Replacing any generation surface (EvalGov conversational answers, prompt-mod authoring, root-cause-analysis narrative text). Those need a frontier LLM; this design doesn't touch them.
- Committing to Jev/TypeSafe AI as a permanent dependency. This is explicitly an experiment behind an abstraction, evaluated with the same evidence discipline the platform already applies to its own routing/prompt changes.
- Redesigning the eval pipeline's sampling/threshold logic — this design only proposes a new evaluator backend, not new sampling policy.

---

## 3. Candidate integration points

### 3.1 M1 — LLM judges (strongest fit)

Most of the 68 metrics are already bounded classification/scoring questions, not generation: correctness, faithfulness, relevance, coherence, hallucination, bias, toxicity, PII detection, prompt-injection detection, tool-selection accuracy, tool-argument accuracy, instruction-following, role adherence. Each maps directly onto Jev's `score` or `bool`-probability question types.

- Add a new evaluator class alongside the existing ones under `evaluators/llm_judges/` (e.g. `evaluators/llm_judges/jev_backend.py`), implementing the same interface the existing judge classes expose (`.evaluate(span, context)` — see `conversation_eval.py`'s `_JUDGES` list for the current shape), but calling Jev's endpoint instead of a chat-completion model.
- `JUDGE_MODEL` in `shadow_eval_pipeline.py` and the per-metric judge wiring stay pointed at the current LLM by default; the Jev-backed evaluator runs as an **additional**, shadow-only evaluator initially (see §4), not a swap.
- If validated, `online_llm_judge_rate` could move toward `1.0` (judge every call instead of a 15% sample) at a fraction of today's cost — this is the concrete payoff, not just lower per-call latency.

### 3.2 M2 — real-time enforcement gates (second-strongest fit)

PII detection, prompt-injection detection, and scope-compliance checks in the gateway's synchronous pre-execution path are latency-sensitive by definition — they gate a live request. A typed bool-with-confidence answer at 70–500ms is a better shape for an inline gate than a full LLM call, and the calibrated confidence score slots directly into the existing numeric trust-score/quality-gate thresholds without needing to parse a model's free-text confidence claim.

- Same pattern as 3.1: introduce as an optional backend for specific enforcement checks, not a replacement of the governance service's existing policy engine.

### 3.3 M4 — proactive monitor anomaly detection (partial fit)

The monitor's detection step ("is this pattern anomalous") is a bounded yes/no-with-confidence decision — candidate for Jev. Its root-cause-analysis step (the generated explanation text surfaced as a finding) is generation and stays on the current LLM. These two steps are already sequential in the monitor's logic, so this only affects the first.

### 3.4 The self-improvement loop's bake-window verify step

`design-backlog/autonomous-self-improvement-loop.md`'s §3.3 bake-window question — "did this change regress quality, and how confident are we" — is itself a typed score/bool decision, not generation. Worth revisiting once that design moves past draft, since it's a clean example of the same pattern applied to the platform's own self-management rather than to agent traffic.

### 3.5 Not a fit

EvalGov's conversational answers, prompt-mod authoring/rewriting, and any root-cause or narrative text generation. Do not attempt to route these through Jev.

---

## 4. Validation approach — dogfooding the platform's own shadow/A-B infrastructure

Rather than trusting vendor-published latency/cost/accuracy claims, treat a Jev-backed judge as just another model behind the existing evidence machinery already documented in `README.md`'s "The self-improving loop":

1. Stand up the Jev-backed evaluator as a **shadow** judge — it scores the same calls the current LLM judges already score, writing to a parallel/tagged set of scores, without affecting production eval results or any downstream gate.
2. Use `compare_shadow_vs_primary` (already used by the gateway agent, `modules/m3-agent-gateway/gateway_agent/tools.py`) to measure agreement rate and score-delta distribution between the Jev judge and the incumbent LLM judge across a representative volume of calls.
3. Only after that evidence clears a bar (to be defined — e.g. agreement within a configured tolerance on a minimum sample size) does a judge-swap or sampling-rate change become a candidate for the **proposed change** queue (`/gateway/changes`) — the same propose → operator-approve → apply path every other change in this platform goes through, per `README.md`'s self-improving-loop section.
4. This is a clean secondary validation of the self-improving loop's generality: the mechanism designed for routing/prompt evidence turns out to also be the right way to evaluate a judge-model swap, with no new infrastructure required.

---

## 5. Risk / caveats

- **Vendor maturity.** TypeSafe AI and Jev are new and early-access at the time of writing. No production track record. Treat as an optional, swappable evaluator backend — never a hard dependency — consistent with how every other model provider in this platform is already swappable (base-URL redirect, no code change).
- **Novel training objective.** Jev's calibration claim (RLCD — "Reinforcement Learning for Calibrated Decisions") is a new, vendor-specific method with no independent long-run validation yet. The shadow-mode comparison in §4 exists precisely to test that claim empirically against this platform's own data rather than accepting it on the vendor's word.
- **Narrow input modality.** Text/JSON only, no images/audio/video — irrelevant to this platform's current text-only eval surface, but a limit to note if multi-modal agent traffic is ever instrumented.
- **Blast radius under the autonomy design.** If a Jev-backed evaluator ever feeds a Tier 2 auto-apply decision (`design-backlog/autonomous-self-improvement-loop.md`), it must clear the same evidence bar as any other input to that decision — a fast, cheap, wrong signal is still a wrong signal, and speed is not a substitute for the validation in §4.

---

## 6. Implementation plan (draft — not started)

### Phase 1 — Shadow evaluator, M1 only
- [ ] New evaluator class under `evaluators/llm_judges/` calling Jev's `/v1/systemone` endpoint, implementing the existing judge interface.
- [ ] Wire it as an additional shadow-only evaluator for a small subset of metrics (start with PII detection and one or two score-type metrics like faithfulness/relevance) — not a replacement.
- [ ] Confirm scores land in a form `compare_shadow_vs_primary`-equivalent tooling can read against the incumbent judge's scores for the same calls.

### Phase 2 — Evidence review
- [ ] Run the shadow comparison over a representative volume/time window.
- [ ] Review agreement rate, score-delta distribution, and actual observed latency/cost against the incumbent judge.
- [ ] Decide, with evidence in hand, whether any metric's sampling rate or judge assignment is worth proposing as a change.

### Phase 3 — M2 real-time gate pilot (only if Phase 2 clears the bar)
- [ ] Same shadow pattern for one inline enforcement check (e.g. PII detection) before touching the synchronous request path.

### Phase 4 — Docs
- [ ] If adopted for any metric, document the evaluator backend choice in `docs/instrumentation-guide.md`/`docs/operator-runbook.md` the same way `JUDGE_MODEL` is documented today.

---

## 7. Open questions

1. Does Jev's `score` question type map cleanly onto every 0–100/0–1 metric this platform already defines, or do some metrics (e.g. multi-turn `knowledge_retention`, which needs cross-turn context) need more input structure than a single typed-schema call naturally provides? Needs a concrete trial per metric, not assumed uniformly.
2. What's the actual latency/cost delta once network overhead to `thejevai.com` is included, versus the vendor's benchmark numbers — only measurable empirically per §4, not assumable from the blog post.
3. Should the Jev evaluator be wired at the same layer as `JUDGE_MODEL` (a single env-var swap), or does it need its own config surface since it's a structurally different API (typed schema request) rather than a drop-in chat-completions call? Leaning toward a separate, explicit evaluator class (as in §3.1) rather than overloading `JUDGE_MODEL`, since the request shape isn't interchangeable with a chat-completions endpoint.
