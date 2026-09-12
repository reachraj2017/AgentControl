# acp-sdk

Umbrella SDK for the AI Control Plane — combines all four module packages into a single install.

Not published to PyPI — install by path, pointed at wherever you cloned the control-plane repo (see its `docs/instrumentation-guide.md` "Prerequisites" section):

```bash
export ACP_REPO=/path/to/the/control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-sdk"                     # all four modules
pip install "$ACP_REPO/sdk/packages/acp-sdk[openai]"            # + OpenAI SDK support
pip install "$ACP_REPO/sdk/packages/acp-sdk[anthropic]"         # + Anthropic SDK support
pip install "$ACP_REPO/sdk/packages/acp-sdk[otel]"              # + OpenTelemetry SDK for richer spans
pip install "$ACP_REPO/sdk/packages/acp-sdk[all]"               # everything
```

## Quick start

```python
from acp_sdk import ACPClient

acp = ACPClient(
    gateway_url="http://localhost:8080",
    eval_runner_url="http://localhost:8000",
    governance_url="http://localhost:8002",
    evalgov_url="http://localhost:8003",
    api_key="gw-sk-...",
    agent_name="summarizer",
    agent_role="summarizer",
    system_id="my-product",
)

# M3 — Route calls through the gateway
client = acp.gateway.openai_client()

# M1 — Instrument with OTLP tracing (auto-evaluated in ClickHouse)
with acp.tracer.span(model="gpt-4o-mini") as ctx:
    ctx.prompt = user_message
    resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": user_message}],
    )
    ctx.completion = resp.choices[0].message.content
    ctx.tokens_in = resp.usage.prompt_tokens
    ctx.tokens_out = resp.usage.completion_tokens

# M2 — Gate sensitive actions with governance
decision = acp.governance.check_policy("send_email", {"recipient": "user@example.com"})
if decision.get("decision") == "block":
    raise PermissionError(decision.get("reason"))

# M4 — Query the control plane conversationally
answer = acp.intelligence.chat("What is the current system health?")
print(answer)

# Checkpoint / handoff / tool-span — signals that never cross the LLM wire
decision = acp.signals.checkpoint("delete_all_records", risk_level="critical")
acp.signals.handoff("orchestrator", "summarizer")
acp.signals.tool_span("web_search", input={"query": "x"}, output={"hits": 3}, latency_ms=180)
```

## Module packages

Each module can also be installed independently when only a subset of modules is deployed:

| Package | Module | What it provides |
|---|---|---|
| `acp-gateway` | M3 | Gateway routing, OpenAI/Anthropic drop-in, traffic pools |
| `acp-tracing` | M1 | OTLP span export, LLM call instrumentation, 68 eval metrics |
| `acp-governance` | M2 | Policy checks, HITL approvals, circuit breakers, trust scores |
| `acp-intelligence` | M4 | EvalGov conversational queries, findings, monitor control |
| `acp-signals` | M2/M1 | Explicit checkpoint/handoff/tool-span calls for signals that never cross the LLM wire — see that package's README |

## ACPClient reference

| Property | Type | Description |
|---|---|---|
| `acp.gateway` | `GatewayClient` | M3 gateway client (lazy-loaded) |
| `acp.tracer` | `ACPTracer` | M1 tracing client (lazy-loaded) |
| `acp.governance` | `GovernanceClient` | M2 governance client (lazy-loaded) |
| `acp.intelligence` | `IntelligenceClient` | M4 EvalGov client (lazy-loaded) |
| `acp.signals` | `SignalsClient` | Checkpoint/handoff/tool-span client (lazy-loaded) |

```python
# Check which modules are reachable
status = acp.health()
# {"gateway": True, "governance": True, "evalgov": True}
```
