# Operator Runbook

Procedures for responding to findings, alerts, and anomalies surfaced by the control plane. Each section covers a specific finding type: what triggered it, how to diagnose it, and how to resolve it.

Findings appear in:
- **Portal** → EvalGov Agent → System Findings panel (right column)
- **EvalGov chat** → ask "Are there any active findings?"
- **MCP** → available to any connected client

---

## Circuit breaker open

**What it means:** An agent exceeded a configurable threshold — error rate, policy violation rate, or rogue behavior score — and the gateway is now blocking all calls from that agent role.

**Immediate impact:** All calls from the affected agent role return an error at the gateway. The agent cannot make LLM calls until the CB is reset.

**Diagnose:**
```
# EvalGov
What caused the [agent-role] circuit breaker to open?
Show me the recent policy violations and gateway errors for [agent-role].
```
Or in the portal: **M2 → Enforcement → Circuit Breakers** → expand the open CB to see the trigger event.

**Resolve:**
1. Identify the root cause (bad prompt, misbehaving tool, cost spike)
2. Fix the underlying issue in the agent code or configuration
3. Reset the CB:
   - Portal: **M2 → Enforcement → Circuit Breakers → Reset**
   - EvalGov: `"Reset the circuit breaker for [agent-role]"`
4. Monitor the next 10–20 calls to confirm the error rate drops

**Do not reset without diagnosing** — if the root cause is not fixed, the CB will re-open immediately.

---

## HITL queue timeout

**What it means:** A pending HITL approval request has been waiting longer than `HITL_TIMEOUT_MINUTES` (default 15 min) without an operator response.

**Immediate impact:** The agent is paused and cannot proceed with the action it requested approval for. Other agent operations may be blocked if the agent is waiting synchronously.

**Diagnose:**
Portal: **M2 → Enforcement → HITL Queue** — shows the pending request with age, agent name, and requested action.

**Resolve:**
- **Approve** if the action is safe: the agent resumes immediately
- **Reject** if the action should not proceed: the agent receives a rejection and should handle it gracefully
- If you're unsure: ask EvalGov — `"What action is [agent-role] waiting for approval on? Is it safe?"`

**Prevent recurrence:** If HITL timeouts are frequent, consider increasing `HITL_TIMEOUT_MINUTES` or setting up alerting for new HITL requests.

---

## Quality gate hold or block

**What it means:**
- **Hold** — an agent's eval score dropped below the warn threshold; the agent is paused pending review
- **Block** — score dropped below the block threshold; the agent is stopped

**Diagnose:**
```
# EvalGov
Show me the eval scores for [agent-role] — which metric triggered the quality gate?
Compare [agent-role] scores today vs the last 7 days.
```
Portal: **M1 → Eval Measurements** → filter by agent role and look for the dropping metric.

**Resolve:**
1. Identify which metric dropped and why (model change, prompt change, data shift)
2. If the drop is legitimate (expected degradation): update the threshold in governance config
3. If the drop is a bug: fix the agent or roll back the deployment
4. Acknowledge the finding in the portal to resume the agent (hold only)
5. For a block, the governance service must receive a `resolve` action before the agent can call the gateway again

---

## Rogue agent detected

**What it means:** An agent's behavior score exceeded the rogue detection threshold — it may be making out-of-scope tool calls, attempting privilege escalation, or exhibiting unexpected behavioral drift.

**Immediate impact:** A `quarantine_recommended = true` flag is set. The gateway applies stricter enforcement for that agent role.

**Diagnose:**
```
# EvalGov
Why was [agent-role] flagged as rogue? Show me the behavioral evidence.
What tool calls or scope violations triggered the rogue detection?
```
Portal: **M2 → AI Governance** → look at the behavioral events for the agent.

**Resolve:**
1. Review the flagged calls in the call log (**M3 → Call Log** → filter by agent role)
2. Determine if the behavior is intentional (update the agent's allowed scope) or a bug (fix and redeploy)
3. If confirmed safe: resolve the rogue assessment via EvalGov — `"The rogue assessment for [agent-role] is a false positive — resolve it"`
4. If confirmed malicious or broken: keep the quarantine in place and fix the root cause before resolving

---

## Critical incident (P0 / P1)

**What it means:** An open incident at P0 (critical) or P1 (high) severity — typically created by the M2 governance agent or manually.

**Diagnose:**
```
# EvalGov
Show me all open P0 and P1 incidents.
What is the timeline of events for the [incident-name] incident?
```

**Resolve:**
1. Investigate and mitigate the root cause
2. Resolve the incident via EvalGov: `"Resolve incident [id] — root cause was X, fixed by Y"`
3. Or via portal: **M2 → AI Governance → Incident Log → Resolve**

**Note:** Resolved incidents are archived but retained in history. They do not re-open automatically.

---

## Low trust score

**What it means:** An agent's trust score dropped below 0.4. This means the agent has accumulated a history of policy violations, safety events, or poor eval performance.

**Immediate impact:** The gateway applies stricter enforcement (lower rate limits, additional policy checks) for that agent role.

**Diagnose:**
```
# EvalGov
Why has [agent-role]'s trust score dropped? Show me the contributing factors.
Show me the policy violation history for [agent-role] over the last 7 days.
```

**Resolve:**
- Trust scores recover automatically over time as the agent makes clean calls
- Fix the underlying issues (bad prompts, out-of-scope tool use) to stop further degradation
- Monitor the trust score trend via EvalGov or **M2 → AI Governance → Trust Scores**

---

## Safety violation detected

**What it means:** A safety event was detected — PII in a response, prompt injection attempt, toxicity, or bias.

**Diagnose:**
Portal: **M2 → AI Governance → Policy Violations** → filter by safety category.
Or: **M3 → Call Log** → find the flagged call and expand to see the safety event detail.

```
# EvalGov
Show me the safety events for [agent-role] in the last hour.
What PII was detected in the [agent-role] agent's responses today?
```

**Resolve:**
1. Review the flagged call content
2. If the agent is leaking PII: update the system prompt or add a prompt mod via the gateway to suppress PII disclosure
3. If it's a prompt injection attempt from user input: review the agent's input validation
4. Acknowledge the finding once mitigated

---

## High gateway error rate

**What it means:** An agent role has a gateway error rate above 20% on at least 5 calls in the last hour.

**Diagnose:**
```
# EvalGov
What errors is [agent-role] getting at the gateway?
Show me the failed gateway calls for [agent-role] in the last hour.
```
Portal: **M3 → Call Log** → filter by agent role and status = error.

**Common causes:**
- Upstream LLM provider outage or rate limiting → check the error message in the call log
- Invalid model name in requests → check routing policy
- Expired or revoked API key → check **M3 → API Keys**
- Budget exhausted → check key spend in **M3 → API Keys**

**Resolve:**
- Provider issue: switch to a fallback model via a routing policy or traffic pool
- Bad key: create a new key in **M3 → API Keys** and deploy it to the agent
- Budget: increase the key's monthly budget in **M3 → API Keys → Update**

---

## Shadow model winning

**What it means:** A shadow model has scored more than 10% higher than the primary model on faithfulness or correctness across at least 20 evaluations in the last 24 hours.

**This is an opportunity, not an error.** It means you may want to promote the shadow model to primary.

**Review:**
```
# EvalGov
Compare the shadow model vs the primary for [agent-role] — show me the score differences and latency.
```
Portal: **M3 → Shadow Mode** → expand the shadow rule to see per-metric comparison.

**Act:**
1. If the shadow model wins on quality and is within acceptable latency/cost: create a routing policy to switch the agent to the shadow model
2. Or run an A/B test to validate at larger scale before fully switching
3. Dismiss the finding once you've made a decision

---

## Stale A/B test

**What it means:** An A/B test has been running for more than 24 hours. Long-running tests accumulate statistical significance but also delay deployment decisions.

**Review:**
```
# EvalGov
What are the current results of the [test-name] A/B test?
Which variant is winning and by how much?
```

**Act:**
- If there's a clear winner (>5% quality difference, >50 calls per variant): stop the test and route to the winner
- If results are inconclusive: let it run longer or stop and re-scope the test
- Stop via EvalGov: `"Stop the [test-name] A/B test"` or via portal: **M3 → A/B Testing → Stop**

---

## Quality regression

**What it means:** An agent's correctness score dropped more than 10% in the last 2 hours compared to the prior 6-hour baseline.

**Diagnose:**
```
# EvalGov
Show me the correctness score trend for [agent-role] over the last 8 hours.
Did anything change in the last 2 hours — model, routing, prompt mods?
```

**Common causes:**
- A routing policy change sent the agent to a lower-quality model
- A new prompt mod is degrading response quality
- The upstream model had a quality regression (check if other agents are affected)
- Data distribution shift in what users are asking

**Resolve:**
1. Roll back the most recent change (routing policy, prompt mod, model)
2. If no recent changes, check if the upstream model degraded by running a manual eval (**M1 → Eval Testing**)
3. Once scores recover, dismiss the finding

---

## Cost spike

**What it means:** Gateway cost in the last hour is more than 2× the 6-hour rolling average.

**Diagnose:**
```
# EvalGov
What is causing the cost spike? Which agents and models are responsible?
Show me cost per call broken down by agent role for the last 2 hours.
```
Portal: **M3 → Gateway Dashboard** → look at cost breakdown by model and agent role.

**Common causes:**
- A routing change sent traffic to a more expensive model
- An agent entered a loop and is making many more calls than usual
- A large prompt mod was added that significantly increases token counts
- A user sent an unusually large prompt

**Resolve:**
- Loop: check the call log for the agent making repeated calls and fix the agent logic
- Routing: review recent routing changes (**M3 → Changes**) and revert if needed
- Budget: set per-key monthly spend limits in **M3 → API Keys** to cap future spikes

---

## Unknown or misconfigured agent role

**What it means:** More than 50% of calls with `agent_role = 'unknown'` are erroring, across at least 10 calls in the last hour. This means agents are hitting the gateway without setting the `X-Gateway-Agent-Role` header.

**Fix:**
Ensure every agent sets the role header. With the SDK:
```python
gw = GatewayClient(gateway_url="...", agent_role="my-agent-role")
```
Without the SDK:
```python
headers = {"X-Gateway-Agent-Role": "my-agent-role", "X-Gateway-System-Id": "my-system"}
```

Without a role, routing policies, governance checks, and traffic management cannot target the agent correctly.

---

## General escalation path

| Severity | Response time | Who acts |
|---|---|---|
| P0 — system down, multiple agents blocked | Immediate | On-call operator |
| P1 — single agent blocked, data leak | < 30 min | On-call operator |
| P2 — quality regression, cost spike | < 2 hours | Team lead |
| P3 — stale test, shadow winning | Next business day | Any operator |
| Info — minor finding, no impact | Monitor and dismiss | Any operator |
