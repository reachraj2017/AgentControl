# acp-intelligence

Conversational interface to the AI Control Plane EvalGov coordinator (M4).

Query the entire control plane in natural language, retrieve proactive findings, trigger monitoring checks, and get live system state — all from within an external agent or application.

## Installation

Not published to PyPI — install by path, pointed at wherever you cloned the control-plane repo (see its `docs/instrumentation-guide.md` "Prerequisites" section):

```bash
export ACP_REPO=/path/to/the/control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-intelligence"
```

## Quick start

```python
from acp_intelligence import IntelligenceClient

intel = IntelligenceClient(evalgov_url="http://localhost:8003")

# Ask EvalGov anything about the control plane
answer = intel.chat("What is the trust score for the summarizer agent?")
print(answer)

# Get active proactive findings
findings = intel.get_findings()
for f in findings:
    print(f["severity"], f["summary"])
    print("  RCA:", f.get("rca", ""))
```

## Multi-turn conversations

```python
history = []

q1 = "Show me the gateway error rates for the last 6 hours"
r1 = intel.chat(q1, history=history)
history += [{"role": "user", "content": q1}, {"role": "assistant", "content": r1}]

q2 = "Why is the error rate high for the translator agent?"
r2 = intel.chat(q2, history=history)
```

## System monitoring

```python
# Trigger all 15 monitor checks immediately
summary = intel.trigger_monitor()
print(summary)
# {"status": "completed", "sources": {"governance": {...}, "gateway": {...}, "quality": {...}}}

# Live system state
state = intel.get_system_state()
print("Open CBs:", state.get("circuit_breakers_open"))
print("Pending HITL:", state.get("pending_hitl_count"))
```

## What EvalGov knows

The EvalGov coordinator delegates to three domain sub-agents:

| Domain | Sub-agent | What you can ask |
|---|---|---|
| M1 — Eval | eval-agent | Eval scores, traces, cost, latency, benchmarks, compliance |
| M2 — Governance | governance-agent | HITL queue, circuit breakers, trust scores, incidents, policy violations |
| M3 — Gateway | gateway-agent | Routing, A/B tests, shadow mode, keys, traffic pools, live stats |

Any natural language question across all three domains works — the coordinator routes automatically.

## Configuration reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `evalgov_url` | str | `http://localhost:8003` | EvalGov coordinator base URL |
| `timeout` | float | `300.0` | HTTP timeout for chat calls (seconds) |
