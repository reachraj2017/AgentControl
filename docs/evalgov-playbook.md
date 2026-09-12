# EvalGov Agent Playbook

EvalGov is the conversational interface to the entire control plane. It delegates to three specialist sub-agents — M1 (eval), M2 (governance), M3 (gateway) — and synthesises answers across all of them automatically.

Access it via:
- **Portal** → EvalGov Agent tab at http://localhost:8888
- **MCP** → `claude mcp add evalgov --transport sse http://localhost:8003/mcp/sse`
- **API** → `POST http://localhost:8003/chat` with `{"message": "...", "history": [...]}`

---

## System health and status

**Get a top-level health check:**
```
What is the current health of the system?
```
EvalGov calls `get_system_health` and synthesises a summary across all modules — open circuit breakers, pending HITL requests, recent policy violations, and quality regressions.

**Check all agents at once:**
```
Show me the trust scores and circuit breaker states for all agents.
```

**Is anything broken right now?**
```
Are there any active findings I should be aware of?
```
Returns all active proactive monitor findings with their severity and RCA.

**Trigger an immediate check:**
```
Run all monitor checks now and tell me what you find.
```
Triggers all 15 monitor checks immediately and returns a per-source summary.

---

## Understanding agent behavior

**Eval scores for a specific agent:**
```
What are the eval scores for the summarizer agent over the last 24 hours?
What is the correctness score trend for the translator agent this week?
Show me the faithfulness and hallucination scores for the orchestrator agent.
```

**Spotting regressions:**
```
Has any agent's quality dropped recently?
Compare the summarizer's eval scores today vs yesterday.
Which agent has the lowest task success rate?
```

**Trace-level detail:**
```
Show me the recent traces for the searcher agent.
What happened on the call with trace ID abc-123?
Which agent calls had errors in the last hour?
```

**Cost and token usage:**
```
How much have my agents spent today?
Which agent is consuming the most tokens?
What is the average cost per call for the summarizer agent?
Show me the cost trend for the last 7 days.
```

---

## Gateway traffic and routing

**Traffic overview:**
```
How many gateway calls were made in the last 6 hours?
What is the cache hit rate today?
What models are being called most frequently?
```

**Routing decisions:**
```
What routing policies are currently active?
Why is the orchestrator agent being routed to gpt-4o-mini instead of gpt-4o?
Show me the routing decisions for the last 100 calls.
```

**Cache performance:**
```
What is the semantic cache hit rate over the last 24 hours?
Show me the most frequently cached prompts.
Are there prompts that should be cached but aren't?
```

**Error analysis:**
```
Which agents have the highest error rate on gateway calls?
Show me all failed calls in the last hour.
What is causing errors for the translator agent?
```

---

## Governance and enforcement

**Policy violations:**
```
Are there any active policy violations?
Show me all policy blocks in the last 24 hours.
Which agents have had the most governance blocks?
```

**Circuit breakers:**
```
Are any circuit breakers currently open?
What caused the summarizer circuit breaker to open?
Reset the circuit breaker for the searcher agent.
```

**HITL queue:**
```
Are there any pending HITL approval requests?
What action is the orchestrator agent waiting for approval on?
How long has the pending HITL request been waiting?
```

**Trust scores:**
```
What is the trust score for each agent?
Which agents have trust scores below 0.5?
Why has the translator agent's trust score dropped?
```

**Incidents:**
```
Are there any open incidents?
Show me all P0 and P1 incidents from the last 7 days.
What was the root cause of the last incident for the orchestrator agent?
```

---

## Traffic management — pools and policies

**View current pools:**
```
Show me all traffic pools and their endpoints.
What endpoints are in the production pool?
Which agents are bound to which pools?
```

**Create a pool:**
```
Create a traffic pool called "gpt4-pool" with a fallback chain strategy.
Add endpoint https://api.openai.com/v1 with model gpt-4o as the primary endpoint in gpt4-pool.
Add endpoint https://api.openai.com/v1 with model gpt-4o-mini as the fallback endpoint in gpt4-pool.
```

**Create a traffic policy:**
```
Bind the summarizer agent role to the gpt4-pool.
Create a traffic policy that routes the orchestrator role to gpt4-pool with sticky sessions enabled.
```

**Live pool stats:**
```
Show me the call counts and error rates for each endpoint in gpt4-pool over the last hour.
Which endpoint in the production pool has the highest latency right now?
```

**Delete and clean up:**
```
Remove the fallback endpoint from gpt4-pool.
Delete the traffic policy for the summarizer agent.
Delete the gpt4-pool.
```
Note: EvalGov will always delete policy first, then pool — deleting a pool with active policies will be flagged.

---

## A/B testing and shadow mode

**A/B test status:**
```
Are there any A/B tests currently running?
What are the results of the gpt4-vs-mini test?
Which variant is winning on quality scores?
Which variant has lower latency?
```

**Create a test:**
```
Create an A/B test for the summarizer agent: send 70% to gpt-4o and 30% to gpt-4o-mini.
```

**Shadow evaluation:**
```
Are there any shadow evaluations running?
How does the shadow model compare to the primary for the orchestrator agent?
Is the shadow model winning on faithfulness?
```

---

## Routing and prompt configuration

**Routing policies:**
```
Show me all routing policies.
Create a routing policy that sends the translator agent to claude-haiku-4-5.
Add a fallback to gpt-4o-mini for the orchestrator agent if latency exceeds 3 seconds.
Delete the routing policy for the summarizer agent.
```

**Prompt mods:**
```
What prompt modifications are currently active?
Create a prompt prefix for the summarizer agent that adds today's date.
Disable the compliance suffix for the orchestrator system.
```

**API keys:**
```
List all active gateway keys.
Create a new gateway key for the translation service with a $50 monthly budget.
What is the spend to date for key gw-sk-abc123?
Revoke the gateway key gw-sk-old-key.
```

---

## Cross-module workflows

These questions require EvalGov to chain multiple sub-agents:

**Diagnosis:**
```
The translator agent is behaving badly — check its eval scores, trust score, and recent gateway errors and tell me what's going on.
```
EvalGov will call M1 for scores, M2 for trust/violations, M3 for gateway errors, and synthesise a unified diagnosis.

**Cost + quality tradeoff:**
```
Which agents have the highest cost per call, and are their quality scores worth it?
```

**Pre-deployment check:**
```
Before I deploy a new version of the summarizer agent, what should I check?
```
EvalGov will surface current baselines, active A/B tests, open CBs, and recent findings relevant to that agent.

**Post-incident review:**
```
The orchestrator agent had an incident last night. Walk me through what happened — eval scores, policy violations, gateway errors, and what the monitor detected.
```

---

## Multi-turn conversations

EvalGov maintains conversation context. You can ask follow-up questions naturally:

```
You: Show me the eval scores for the summarizer agent.
EvalGov: [shows scores — correctness 0.72, faithfulness 0.68...]

You: Why is faithfulness so low?
EvalGov: [delegates to M1 with full context — examines recent traces, compares to baseline...]

You: What should I do about it?
EvalGov: [recommendations based on M1 + M2 findings...]

You: Go ahead and create a prompt mod to address the faithfulness issue.
EvalGov: [delegates to M3 gateway agent to create the prompt mod...]
```

---

## New in v4 — checking a freshly-instrumented, gateway-only agent

Since v4, an agent doesn't need any ACP tracing SDK in-process to get fully evaluated — routing its calls through the gateway is enough. These questions use EvalGov's existing eval-score and gateway-traffic tools, just framed for that check:

```
Is the new-inventory-agent showing up in eval scores yet?
What eval scores exist for system_id "my-new-system" in the last hour?
How many gateway calls has my-new-system made, and what's its error rate?
```
If you see gateway calls in M3 but no eval scores in M1 after ~90 seconds, that's the actual signal something's wrong with ingestion — see `docs/instrumentation-guide.md`'s validation section for what to check next (compare `gateway_call_log` tokens against `prompt_evals` tokens for the same call).

**Not yet queryable via EvalGov:** checkpoint/handoff/tool-span structural signals (from `acp-signals`' `checkpoint()`/`handoff()`/`tool_span()` calls) are stored in `gateway_structural_events`, but no M1/M2/M3 sub-agent tool currently reads that table — EvalGov can't yet answer "show me recent handoffs for my agent." Query ClickHouse directly for these until that's added.

---

## Tips

- **Be specific about time windows** — "last 24 hours" is more useful than "recently"
- **Name agents by role** — use the `agent_role` tag (e.g. "summarizer", "orchestrator") not service names
- **Chain actions in one message** — "check the trust score for the summarizer and if it's below 0.5, reset its circuit breaker"
- **Use the findings panel** — the right-hand panel in the portal shows active findings without needing to ask
- **MCP from Claude Code** — once connected via MCP, you can ask EvalGov questions inline while writing agent code without switching to the portal
