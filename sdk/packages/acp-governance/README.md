# acp-governance

Policy checks, HITL approvals, circuit breakers, trust scores, and incident management via the AI Control Plane Governance Service (M2).

## Installation

Not published to PyPI — install by path, pointed at wherever you cloned the control-plane repo (see its `docs/instrumentation-guide.md` "Prerequisites" section):

```bash
export ACP_REPO=/path/to/the/control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-governance"
```

## Quick start

```python
from acp_governance import GovernanceClient

gov = GovernanceClient(
    governance_url="http://localhost:8002",
    agent_name="summarizer",
    system_id="my-product",
)

# Gate a sensitive action behind a policy check
decision = gov.check_policy(
    action="send_email",
    context={"recipient": "user@example.com", "data_classification": "pii"},
)
if decision.get("decision") == "block":
    raise PermissionError(f"Action blocked by governance: {decision.get('reason')}")

# Check the circuit breaker before making calls
if gov.is_open():
    raise RuntimeError("Circuit breaker is OPEN — agent is paused by governance")

# Trust score
score = gov.get_trust_score()
if score < 0.5:
    print(f"Warning: low trust score {score:.2f}")
```

## Human-in-the-loop approvals

```python
# Request approval for a destructive action
req = gov.request_approval(
    action="delete_all_records",
    context={"table": "users", "count": 50000},
    reason="End-of-retention cleanup",
)

# Option A — poll manually
status = gov.get_approval_status(req["id"])
if status["status"] == "approved":
    run_deletion()

# Option B — block until decided (or timeout)
try:
    result = gov.wait_for_approval(req["id"], timeout=300.0)
    if result["status"] == "approved":
        run_deletion()
except TimeoutError:
    print("Approval not received within 5 minutes — skipping")
```

## Incident and safety reporting

```python
# Create a governance incident
gov.create_incident(
    title="Unexpected PII in response",
    severity="P1",
    description="Agent returned user email address in output",
)

# Report a safety event
gov.report_safety_event(
    event_type="pii_leak",
    details="Email address detected in completion",
    detected=True,
)
```

## Configuration reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `governance_url` | str | `http://localhost:8002` | ACP Governance Service base URL |
| `agent_name` | str | `"agent"` | Agent name for trust scores, CBs, policy decisions |
| `system_id` | str | `"*"` | System/deployment identifier |
