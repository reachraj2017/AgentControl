# M4 — Intelligence (EvalGov Agent)

M4 is the primary user interface for the entire AI Control Plane. It operates as a coordinator agent that orchestrates three domain-specific sub-agents (M1, M2, M3), runs a proactive background monitor, and exposes an MCP server for external tool integrations.

## Architecture

EvalGov uses a coordinator + sub-agent pattern:

```
User / Portal
     │
     ▼
EvalGov Coordinator (port 8003)
  ├── call_eval_agent       → M1 Eval Agent (port 8001)
  ├── call_governance_agent → M2 Governance Agent (port 8004)
  ├── call_gateway_agent    → M3 Gateway Agent (port 8005)
  ├── get_system_health     (direct — governance-service)
  └── get_proactive_observations (direct — /observations endpoint)
```

**Coordinator** carries 5 tools. It owns reasoning, routing decisions, and response synthesis. The MCP server and proactive monitor both live here. On follow-up turns (history non-empty) the coordinator skips `get_proactive_observations` and delegates directly — keeping response times fast. When passing a follow-up question to a sub-agent, it rephrases the query with full context so the sub-agent does not need the conversation history.

**Sub-agents** are stateless HTTP services. Each one receives a query + optional history, runs its own LLM tool loop against its domain's tools, and returns a plain-text response. They have no persistent state and no MCP server.

**Model selection** — all four agents read `AGENT_MODEL` from the environment (default: `anthropic/claude-sonnet-4-6`).

## What it does

### Conversational governance

- Chat with the coordinator in natural language — it routes your question to the right sub-agent(s) automatically
- Cross-module queries chain multiple sub-agents in sequence (e.g. "check eval scores then fix routing")
- For broad status questions, the coordinator calls `get_system_health` first, then drills into sub-agents as needed

### Proactive monitor

A background asyncio loop (`monitor.py`) polls all system signals every 60 seconds (configurable via `MONITOR_POLL_SECONDS`). When an anomaly is detected it generates a finding with LLM root cause analysis and stores it in ClickHouse.

#### Checks (15 total)

**🏛️ Governance** — queries the governance-service HTTP API

| Check | Trigger condition |
|---|---|
| Circuit breaker open | Any CB with `state = OPEN` |
| HITL quality gate hold | Any `quality_gate_hold` entry in the pending HITL queue (fires immediately) |
| HITL quality gate block | Any `quality_gate_block` entry in the pending HITL queue (fires immediately) |
| HITL timeout | Any other pending HITL request waiting ≥ `HITL_TIMEOUT_MINUTES` (default 15) |
| Rogue agent | Any agent with `quarantine_recommended = true` |
| Critical incident | Any open P0 or P1 incident |
| Low trust score | Any agent with `trust_score < 0.4` |
| Safety violation | Any safety event with `detected = true` in the last hour, grouped by agent |
| Policy hard block | Any `decision = 'block'` in `gov_policy_decisions` in the last hour |
| Statistical anomaly | Any metric with z-score ≥ 3 in the last hour |
| Behavior violation | Any `scope_violation`, `policy_violation`, or `capability_abuse` event in the last hour |

**🌐 Gateway** — queries ClickHouse directly (`gateway_call_log`, `gateway_shadow_evals`, `gateway_ab_tests`)

| Check | Trigger condition |
|---|---|
| High routing error rate | Any agent with > 20% error rate on ≥ 5 gateway calls in the last hour |
| Unknown/misconfigured role | `agent_role = 'unknown'` with > 50% error rate on ≥ 10 calls in the last hour |
| Shadow model winning | Shadow model faithfulness > 10% higher than primary over ≥ 20 evals in last 24h |
| Stale A/B test | Any A/B test with `status = 'running'` for more than 24 hours |

**📊 Quality & Cost** — queries ClickHouse directly (`eval_scores` + `prompt_evals`, `gateway_call_log`)

| Check | Trigger condition |
|---|---|
| Quality regression | Any agent's `correctness` score drops > 10% in the last 2h vs the prior 6h baseline (JOIN `eval_scores` → `prompt_evals` on `span_id` to get agent name) |
| Cost spike | Last hour gateway cost > 2× the 6-hour rolling average (computed as `tokens_in × $0.15/1M + tokens_out × $0.60/1M`) |

#### RCA generation

Every new finding triggers `generate_rca()` in `agent.py` — a direct LiteLLM call to the configured Claude model. It receives a JSON blob describing the anomaly type, affected agent, description, and raw signal data, and returns:

```json
{
  "severity":       "critical | high | warning | medium | info",
  "summary":        "One-sentence what happened",
  "rca":            "Root cause explanation",
  "recommendation": "Actionable next step"
}
```

All four fields are stored in `otel.gov_agent_findings` alongside the raw signal data. If the RCA call fails, the finding is still created using the monitor's `default_severity` and a fallback message.

#### Deduplication

State-based: a finding is only created when there is **no existing `active` or `acknowledged` finding** for the same `(finding_type, affected_agent)` pair. This prevents the panel from filling with duplicate rows for persistent conditions (e.g. an open CB that hasn't been reset).

- **Dismiss** (`acknowledged`) — marks the finding as seen; the monitor will not re-create it while it remains acknowledged. The underlying condition is still tracked.
- **↩ Reopen** — marks the finding as `resolved`, which allows the monitor to re-detect the condition on the next cycle and generate a fresh RCA.
- **Resolved** — set manually via the governance-agent or the Reopen flow. Opens the door for re-detection.

Findings are stored in `otel.gov_agent_findings` (ClickHouse `ReplacingMergeTree(updated_at)`, deduped on `finding_id`).

**Manual trigger** — `POST /monitor/run` runs all 15 checks immediately and returns a per-source summary:

```json
{
  "status": "completed",
  "sources": {
    "governance": {"detected": 3, "new_findings": 1},
    "gateway":    {"detected": 0, "new_findings": 0},
    "quality":    {"detected": 1, "new_findings": 0}
  }
}
```

`detected` = anomalies found; `new_findings` = findings actually created (rest were suppressed by dedup). Available as the **▶ Run Checks** button in the portal — shows a spinner while in flight (can take 30–60s when new findings trigger RCA calls).

### Portal findings panel

The EvalGov portal page shows a **System Findings** panel (right column, auto-refreshes every 60s) with three sections:

- **🏛️ Governance** — CB, HITL, rogue, incidents, trust, safety, policy, anomaly, behavior findings
- **🌐 Gateway** — routing error rate, unknown role, shadow winning, stale A/B test findings
- **📊 Quality & Cost** — correctness regression and cost spike findings

Active findings are shown at full opacity; acknowledged (dismissed) findings are shown at 50% opacity with a **(seen)** label and a **↩ Reopen** button instead of Dismiss. This ensures persistent conditions (e.g. an open CB) remain visible even after being acknowledged.

Expanding **↳ RCA + Recommendation** on any finding reveals the LLM-generated root cause analysis, recommendation, and raw signal JSON.

### MCP server

- Exposes the coordinator's tools via the Model Context Protocol (SSE transport) at `/mcp/sse`
- Any MCP-compatible client (Claude Code, Claude Desktop, IDEs) can connect and query the control plane conversationally

### Human-in-the-loop

- Agent pauses and requests operator approval before taking irreversible actions
- Approval/rejection tracked in the governance HITL queue

## Sub-agents

### M1 Eval Agent (port 8001)

Domain tools (~23): traces, performance metrics, costs, error rates, safety events, thresholds, budgets, version pins, lifecycle changes, model registry, compliance scorecard, risk register, prompt detail, search prompts, eval runs, benchmarks, eval scores, list/create benchmarks, create/execute/trigger/baseline eval runs.

### M2 Governance Agent (port 8004)

Domain tools (~25): HITL queue/approve/reject/bulk, circuit breakers/reset/quarantine, trust scores, rogue assessments, incidents/resolve/bulk, anomalies, burn rates, quality gates, gate audit log, policy violations, policy decisions, findings/acknowledge/resolve/bulk, compliance report, reliability summary.

### M3 Gateway Agent (port 8005)

Domain tools (~33): gateway call stats, routing decisions, proposals, A/B tests (list/create/stop/delete/results), shadow vs primary comparison, gateway keys (list/create/update/revoke), key rejection events, routing policies (list/create/delete), prompt mods (list/create/delete), shadow rules (list/create/delete), **traffic management** — endpoint pools (list/create/delete), pool endpoints (add/remove), traffic policies (list/create/delete), live pool traffic stats.

## Dependencies

Requires M1 (ClickHouse, eval-runner) and M2 (governance-service). M3 (agent-gateway) required for gateway-agent sub-agent. All three sub-agents must be healthy before evalgov-agent starts.

## Run full stack

```bash
make up
```

## MCP connect (Claude Code)

```bash
claude mcp add evalgov --transport sse http://localhost:8003/mcp/sse
```

Verify:
```bash
claude mcp list
```

## MCP connect (other clients)

```json
{
  "mcpServers": {
    "evalgov": {
      "url": "http://localhost:8003/mcp/sse"
    }
  }
}
```

## Key endpoints

| Path | Method | Description |
|------|--------|-------------|
| `/health` | GET | Health check |
| `/chat` | POST | Send message to coordinator |
| `/findings/active` | GET | Active findings grouped by severity |
| `/findings/{id}/acknowledge` | POST | Acknowledge a finding |
| `/findings/{id}/resolve` | POST | Resolve a finding |
| `/findings/acknowledge-bulk` | POST | Bulk acknowledge by `{ids: [...]}` |
| `/monitor/run` | POST | Trigger all checks immediately; returns per-source summary |
| `/observations` | GET | Active findings in legacy observation format (coordinator tool) |
| `/system-state` | GET | Live snapshot: HITL queue, policy violations, incidents, CBs |
| `/mcp/sse` | GET | MCP SSE stream |

## Configuration

| Variable | Description |
|----------|-------------|
| `ANTHROPIC_API_KEY` | Required — powers all four agents |
| `AGENT_MODEL` | LLM model for all agents (default: `anthropic/claude-sonnet-4-6`) |
| `MONITOR_POLL_SECONDS` | Monitor poll frequency in seconds (default: `60`) |
| `HITL_TIMEOUT_MINUTES` | Minutes before a pending HITL request becomes a finding (default: `15`) |
| `GOVERNANCE_SERVICE_URL` | Governance API base URL (default: `http://governance-service:8002`) |
| `GATEWAY_URL` | Gateway base URL (default: `http://agent-gateway:8080`) |
| `GATEWAY_MASTER_KEY` | Gateway admin key — required for gateway-agent tools |
| `EVAL_AGENT_URL` | M1 sub-agent URL (default: `http://eval-agent:8001`) |
| `GOVERNANCE_AGENT_URL` | M2 sub-agent URL (default: `http://governance-agent:8004`) |
| `GATEWAY_AGENT_URL` | M3 sub-agent URL (default: `http://gateway-agent:8005`) |

## Key ports

| Service | Port |
|---------|------|
| evalgov-agent (coordinator) | 8003 |
| eval-agent (M1 sub-agent) | 8001 |
| governance-agent (M2 sub-agent) | 8004 |
| gateway-agent (M3 sub-agent) | 8005 |
| portal | 8888 |
