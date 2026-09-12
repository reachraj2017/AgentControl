# Roadmap: Capability Expansion & Maturity

**Status:** Survey / backlog — not prioritized, not committed
**Date:** 2026-09-06
**Author:** Raj Ramanujam
**Scope:** Local planning doc. Not for the public repo / README until accepted.

---

## 1. Context

This is a review of the AI Control Plane (M1 Observability & Eval, M2 Governance & Enforcement,
M3 Agent Gateway, M4 EvalGov Intelligence) as of 2026-09-04, done to answer: *what further
capabilities would expand and mature this system?* It builds on two things already in the repo:

- `docs/external-agent-integration-findings.md` — six issues found instrumenting an external
  agent system (openai-cs-agents-demo) against the gateway.
- `design/v2-gateway-capture-m1-ingest.md` — the design already drafted to fix the biggest of
  those issues (gateway-only traffic produces no eval data).

That design doc is the highest-leverage next step and should land before most items below,
since several of them depend on gateway-captured data actually being real (~55/68 metrics
functional gateway-only once implemented, per that doc's §6).

---

## 2. What's already strong (baseline, not gaps)

A real coordinator/sub-agent architecture (M4 → M1/M2/M3 agents), 68-metric eval pipeline,
13-category policy engine with circuit breakers and HITL, a gateway with routing/caching/A-B/
shadow/traffic-pools, and a proactive anomaly monitor with LLM-generated RCA. Past "demo" stage —
genuine multi-service control plane with real data flow between modules.

---

## 3. Per-module capability ideas

### M1 — Observability & Eval
- **Protocol completeness**: `/v1/responses`, native `/v1/messages`, Gemini `generateContent`.
  Without these, OpenAI Agents SDK (hosted tools), Claude Agent SDK, and ADK-native traffic
  never reach the gateway at all (Issue 3 in the findings doc). Tracked as Phase 4 in the
  gateway-ingest design doc.
- **Golden-dataset regression CI** — benchmark runs + regression comparison exist in the portal,
  but nothing wired to CI to block a merge on a metric regression; currently a human has to look.
- **Eval judge diversity / hard-fail mode** — LLM-judge metrics silently fall back to 0.5 if
  `ANTHROPIC_API_KEY` is bad or missing (flagged as the most impactful pre-existing gap in the
  findings doc). Worth an ensemble/majority-vote judge option, and a hard failure instead of a
  silent neutral score.
- **Cost-attributed eval** — correctness-per-dollar, or a score normalized by token cost, useful
  as an input to M3 routing decisions.

### M2 — Governance & Enforcement
- **Policy-as-code** — a declarative policy DSL (OPA/Rego-style or a YAML policy compiler) to
  version, diff, and test policies instead of editing config directly.
- **Fail-open → fail-closed toggle per category** — enforcement is fail-open by default and this
  isn't surfaced clearly to operators (findings doc). A per-category setting, visible in the
  portal, would let operators consciously choose blast radius (e.g. fail-closed on PII, fail-open
  on style).
- **Approval delegation / escalation chains for HITL** — currently a flat queue; multi-tier
  approval and SLA-based auto-escalation matter as usage scales.
- **Policy simulation / dry-run** — "what would this policy have blocked over the last 7 days"
  before activating it live.

### M3 — Agent Gateway
- **Non-Python SDK story** — capture is HTTP so it's protocol-complete in principle, but there's
  no documented TS/JS/Go client snippet. A large fraction of agent stacks are TS.
- **Streaming-aware caching** — semantic cache is currently skipped for tool-call responses;
  extend to cache streamed responses as traffic volume grows.
- **Per-tenant isolation** — virtual keys exist, but no concept of a "tenant"/"project" scoping
  data or dashboards. Needed before multiple teams/customers share one instance.
- **Pre-flight budget estimation** — budget checks happen per-call after the fact; a pre-flight
  cost estimator (e.g. for long-context calls) would prevent a single call from blowing a budget
  cap.

### M4 — Intelligence
- **Cross-module correlation** — the monitor runs 15 independent checks per source; a
  correlation layer (e.g. "cost spike AND correctness regression on the same agent at the same
  time" is a stronger signal than either alone) would catch compound incidents faster.
- **Feedback loop from RCA → policy** — RCA is generated and shown to a human today; let EvalGov
  *propose* a governance policy change from a recurring finding (with human approval) to close
  the loop from detection to remediation.
- **Longer-horizon trend memory** — current windows are 1h/2h/6h/24h; no "this is the third time
  this week" pattern detection across days/weeks.

---

## 4. Cross-cutting / platform maturity

- **Deployment** — only `docker-compose` (4 tiered files) exists; no Helm chart / k8s manifests,
  no HA story for ClickHouse or the agent services. Matters once this leaves a single dev
  machine.
- **AuthN/AuthZ** — `GATEWAY_MASTER_KEY` and virtual `gw-sk-*` keys cover gateway access, but no
  visible RBAC on the portal itself (who can approve HITL, edit policies, revoke keys). Anyone
  with portal access can currently do anything.
- **Secrets management** — API keys live in `.env`; no vault/secrets-manager integration beyond
  local dev.
- **Data retention & cost** — full prompt/response payloads (`messages_json` etc.) accumulating
  in ClickHouse with no TTL policy (flagged in the gateway-ingest design doc §10.4). Will become
  a real storage-cost and PII-retention problem at volume.
- **Multi-tenancy** — no tenant/org boundary anywhere in the schema; everything is implicitly
  single-tenant.
- **Conformance testing** — the gateway-ingest design doc's own Phase 5 (golden fixtures per
  framework, asserting eval rows appear) is exactly the regression net that would have caught
  all six integration issues before a live test did. Probably the highest-value maturity
  investment relative to effort — turns "does instrumentation actually work" from a manual,
  expensive discovery process into CI.
- **Alerting egress** — findings currently surface only in the portal / EvalGov chat; no
  Slack/PagerDuty/webhook sink for critical findings, which matters once this isn't being watched
  interactively.

---

## 5. Suggested sequencing (not committed)

1. Land `design/v2-gateway-capture-m1-ingest.md` (gateway-only traffic → real evals).
2. Conformance suite (that design doc's Phase 5) — regression net for future changes.
3. Protocol completeness (`/v1/responses`, `/v1/messages`, Gemini) — unblocks the frameworks most
   commonly hit in the wild.
4. RBAC + secrets management — before this is shared beyond a single operator.
5. Multi-tenancy + data retention policy — before multiple teams/customers share one instance.
6. Everything else (policy-as-code, cross-module correlation, non-Python SDKs, HA deployment) —
   pull forward as specific need arises.

---

## 6. Open questions

- Priority order above is a guess based on blast-radius and dependency, not a commitment — revisit
  once the gateway-ingest design doc actually lands and its real gaps become clearer.
- Some items (multi-tenancy, RBAC) are only worth doing if this is heading toward multi-operator
  or multi-customer use; if it stays single-operator/single-team, they can stay backlog
  indefinitely.
