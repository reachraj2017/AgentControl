# acp-gateway

Route LLM calls through the AI Control Plane Agent Gateway (M3).

The gateway is a drop-in proxy for OpenAI and Anthropic APIs. It transparently adds:
- **Routing** — model-level routing policies with fallback chains
- **Caching** — exact-match and semantic (embedding-based) response caches
- **A/B testing** — traffic-split experiments with per-variant tracking
- **Shadow mode** — duplicate traffic to a secondary model without affecting responses
- **Traffic management** — endpoint pools with 6 load-balancing strategies
- **Governance enforcement** — per-key budgets, rate limits, model allowlists, and circuit breakers
- **Observability** — every call logged to ClickHouse with full token counts, latency, and routing reason

## Installation

Not published to PyPI — install by path, pointed at wherever you cloned the control-plane repo (see its `docs/instrumentation-guide.md` "Prerequisites" section):

```bash
export ACP_REPO=/path/to/the/control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-gateway"                    # httpx only — use GatewayClient directly
pip install "$ACP_REPO/sdk/packages/acp-gateway[openai]"           # + openai SDK drop-in support
pip install "$ACP_REPO/sdk/packages/acp-gateway[anthropic]"        # + anthropic SDK drop-in support
pip install "$ACP_REPO/sdk/packages/acp-gateway[all]"              # both
```

## Quick start

### Direct API calls

```python
from acp_gateway import GatewayClient

gw = GatewayClient(
    gateway_url="http://localhost:8080",
    api_key="gw-sk-...",        # virtual gateway key (optional if auth disabled)
    agent_role="summarizer",    # used for routing policies and observability
    system_id="my-product",     # scopes policies to this deployment
)

# OpenAI-compatible call
resp = gw.chat_openai(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Summarize this text: ..."}],
)
print(resp["choices"][0]["message"]["content"])

# Anthropic-compatible call
resp = gw.chat_anthropic(
    model="claude-haiku-4-5",
    messages=[{"role": "user", "content": "Summarize this text: ..."}],
)
print(resp["content"][0]["text"])
```

### OpenAI SDK drop-in

```python
from acp_gateway import GatewayClient

gw = GatewayClient(gateway_url="http://localhost:8080", agent_role="summarizer")

# Point the official OpenAI SDK at the gateway — zero other changes needed
client = gw.openai_client()
resp = client.chat.completions.create(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": "Hello!"}],
)
```

### Anthropic SDK drop-in

```python
from acp_gateway import GatewayClient

gw = GatewayClient(gateway_url="http://localhost:8080", agent_role="translator")

# Point the official Anthropic SDK at the gateway — zero other changes needed
client = gw.anthropic_client()
resp = client.messages.create(
    model="claude-haiku-4-5",
    max_tokens=1024,
    messages=[{"role": "user", "content": "Hello!"}],
)
```

### Sticky sessions (multi-turn conversations)

When a traffic policy has `sticky=true`, all turns of a conversation must hit the same pool endpoint. Pass a stable `conversation_id`:

```python
gw = GatewayClient(
    gateway_url="http://localhost:8080",
    agent_role="chatbot",
    conversation_id="session-abc-123",   # same ID across all turns
)
```

## Gateway headers

Every request adds these headers, which the gateway reads for routing and governance:

| Header | Value |
|---|---|
| `X-Gateway-Agent-Role` | The `agent_role` you passed to `GatewayClient` |
| `X-Gateway-System-Id` | The `system_id` you passed (default `*`) |
| `X-Gateway-Conversation-Id` | Set when `conversation_id` is non-empty |
| `Authorization: Bearer` | Set when `api_key` is non-empty |

## Gateway status

```python
# Check connectivity
if not gw.health():
    raise RuntimeError("gateway is down")

# Call stats for the last 24 hours
stats = gw.get_call_stats(hours=24)

# List endpoint pools
pools = gw.get_traffic_pools()
```

## Environment-variable pattern

For production use, read credentials from environment variables instead of hardcoding:

```python
import os
from acp_gateway import GatewayClient

gw = GatewayClient(
    gateway_url=os.environ.get("ACP_GATEWAY_URL", "http://localhost:8080"),
    api_key=os.environ.get("ACP_GATEWAY_KEY", ""),
    agent_role=os.environ.get("ACP_AGENT_ROLE", "default"),
    system_id=os.environ.get("ACP_SYSTEM_ID", "*"),
)
```

## Configuration reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `gateway_url` | str | `http://localhost:8080` | Base URL of the ACP Agent Gateway |
| `api_key` | str | `""` | Virtual gateway API key (`gw-sk-*`) — required when `GATEWAY_AUTH_ENABLED=true` |
| `agent_role` | str | `"default"` | Role tag — used for routing, traffic management, governance, observability |
| `system_id` | str | `"*"` | System/deployment identifier — scopes policies |
| `conversation_id` | str | `""` | Stable session ID for sticky traffic policies |
