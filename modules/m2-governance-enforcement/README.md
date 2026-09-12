# M2 — Governance & Enforcement

M2 adds a real-time policy engine on top of M1. Every agent action is evaluated against a 13-category governance framework before and after execution, with configurable thresholds, human-in-the-loop escalation, and circuit breakers.

## What it does

- Runs the governance-service, which exposes a synchronous policy check API consumed by agent SDKs and the gateway
- Enforces 13 governance categories: safety, PII, prompt injection, identity, budget, quality, compliance, behavior, reliability, model routing, regulatory, anomaly, and drift
- Maintains per-agent trust scores and lifecycle state
- Triggers circuit breakers when failure thresholds are exceeded — circuit breaker state is visible in both the Enforcement page and the Gateway Dashboard
- Supports human-in-the-loop (HITL) review for borderline decisions; HITL queue visible and actionable via portal and EvalGov agent chat

## Portal pages

| Page | What you can do |
|------|----------------|
| AI Governance | Trust scores per agent, policy violation history, incident log |
| Enforcement | Circuit breaker state (open/half-open/closed), HITL queue management |

## Dependencies

Requires M1 (ClickHouse, eval-runner).

## Run with M1

```bash
make up-m1-m2
```

## Instrumenting external agents

Use the `acp-governance` SDK to add policy checks, HITL approvals, and circuit breaker queries to any external agent:

```bash
# Not published to PyPI — install by path (see docs/instrumentation-guide.md "Prerequisites")
pip install "/path/to/this/repo/sdk/packages/acp-governance"
```

See [`sdk/packages/acp-governance/README.md`](../../sdk/packages/acp-governance/README.md) for full usage.

## M2 sub-agent (governance-agent)

In addition to the governance-service, M2 includes a domain-specific LLM agent that the EvalGov coordinator delegates to for all governance-related questions and actions.

**What it handles:** HITL queue (view/approve/reject/bulk), circuit breaker state and resets, agent quarantine, trust scores, rogue assessments, incidents (open/resolve/bulk), anomaly detection, burn rates, quality gate decisions, gate audit log, policy violations, policy decisions, proactive monitor findings (acknowledge/resolve/bulk), compliance reports, reliability summaries.

The sub-agent is stateless — it receives a query + optional history, runs its own LLM tool loop (~25 tools), and returns a plain-text response. It starts automatically as part of `make up` and requires `governance-service` to be healthy first.

## Key ports

| Service                        | Port |
|--------------------------------|------|
| governance-service             | 8002 |
| governance-agent (M2 sub-agent) | 8004 |
| portal                         | 8888 |
