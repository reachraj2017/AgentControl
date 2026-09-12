# acp-tracing

Instrument LLM calls with OTLP spans for the AI Control Plane (M1 — Observability & Eval).

Every span the tracer emits is automatically picked up by the ACP eval-runner, which runs **68 evaluation metrics** (correctness, faithfulness, tone, safety, cost, latency, PII detection, and more) and stores results in ClickHouse. Results appear immediately in the portal and are queryable via the EvalGov conversational interface.

## Installation

Not published to PyPI — install by path, pointed at wherever you cloned the control-plane repo (see its `docs/instrumentation-guide.md` "Prerequisites" section):

```bash
export ACP_REPO=/path/to/the/control-plane-repo
pip install "$ACP_REPO/sdk/packages/acp-tracing"                  # httpx fallback exporter only
pip install "$ACP_REPO/sdk/packages/acp-tracing[otel]"            # + opentelemetry-sdk for richer spans
```

## Quick start

### Context manager

```python
from acp_tracing import ACPTracer

tracer = ACPTracer(
    otlp_endpoint="http://localhost:8000",   # ACP eval-runner
    agent_name="summarizer",
    agent_role="summarizer",
    system_id="my-product",
)

with tracer.span(model="gpt-4o-mini") as ctx:
    ctx.prompt = user_message
    response = openai_client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": user_message}],
    )
    ctx.completion = response.choices[0].message.content
    ctx.tokens_in = response.usage.prompt_tokens
    ctx.tokens_out = response.usage.completion_tokens
# span exported on exit — eval-runner processes it within seconds
```

### One-shot (already have response data)

```python
ctx = tracer.record_call(
    model="claude-haiku-4-5",
    prompt=prompt_text,
    completion=response_text,
    tokens_in=512,
    tokens_out=128,
    latency_ms=340.5,
)
print("span_id:", ctx.span_id)
```

### Decorator

```python
@tracer.trace(model="gpt-4o-mini")
def summarize(text: str) -> str:
    response = client.chat.completions.create(...)
    return response.choices[0].message.content
```

### Multi-turn conversations

```python
with tracer.span(model="gpt-4o-mini", conversation_id="session-abc-123") as ctx:
    ...
```

The `conversation_id` attribute is stored on the span and used by the portal's Call Log to group turns.

## OTLP export strategy

The tracer checks for `opentelemetry-sdk` on startup:
- **If installed** — uses the official OTLP/HTTP exporter with a `BatchSpanProcessor` (richer, supports all OTel backends)
- **If not installed** — falls back to a direct `httpx` POST of a minimal OTLP-JSON payload directly to the eval-runner

Tracing **never raises exceptions** — all export errors are silently swallowed so instrumentation cannot crash the host application.

## Configuration reference

| Parameter | Type | Default | Description |
|---|---|---|---|
| `otlp_endpoint` | str | `http://localhost:8000` | ACP eval-runner base URL |
| `agent_name` | str | `"agent"` | Span name — shown as the agent identifier in the portal |
| `agent_role` | str | `"default"` | Role tag for routing and governance correlation |
| `system_id` | str | `"*"` | System/deployment identifier |
| `service_name` | str | `"acp-instrumented-agent"` | OTel `service.name` resource attribute |

## SpanContext fields

After a span exits, the `SpanContext` carries:

| Field | Type | Description |
|---|---|---|
| `span_id` | str | UUID for this span |
| `trace_id` | str | UUID for the trace |
| `prompt` | str | The LLM input |
| `completion` | str | The LLM output |
| `model` | str | Model name |
| `tokens_in` | int | Prompt token count |
| `tokens_out` | int | Completion token count |
| `latency_ms` | float | Wall-clock call duration |
| `error` | str | Error message if the span raised an exception |

## Recommended path for external / custom agent systems: bring your own OTel instrumentor

`ACPTracer` / `instrument()` above are the path used by ACP's own bundled demo (`opt-demo`) and
existing in-process integrations, and they keep working exactly as documented. For a **new**
external or custom agent system, the recommended path is different: don't ask this package to
instrument your framework. Instead, point a standard OTLP exporter at the ACP collector and let a
community-maintained instrumentor from the **OpenInference** or **OpenLLMetry** ecosystem do the
actual per-framework work.

Why: passive, ACP-owned instrumentation (patching a framework's client, registering a competing
global tracer) is fragile against a framework's own internals and its own telemetry in a way a
community-maintained instrumentor, built and kept current by people who track that framework
full-time, is not. OpenInference (Arize) and OpenLLMetry (Traceloop) already maintain
auto-instrumentors for OpenAI, Anthropic, Google/Vertex, LangChain, LlamaIndex, CrewAI, Bedrock, and
more, emitting OTel GenAI semantic-convention-aligned spans that M1's ingestion pipeline reads
natively. That maintenance burden belongs to those communities, not to this package.

```bash
# 1. Install a community instrumentor for your framework/provider:
pip install openinference-instrumentation-openai
# — or, for OpenLLMetry instead:
pip install traceloop-sdk
```

```python
from acp_tracing import configure_otlp_exporter
configure_otlp_exporter(endpoint="http://localhost:4318")

# OpenInference:
from openinference.instrumentation.openai import OpenAIInstrumentor
OpenAIInstrumentor().instrument()

# OR OpenLLMetry (manages its own exporter — pass the endpoint directly instead):
# from traceloop.sdk import Traceloop
# Traceloop.init(app_name="my-agent", api_endpoint="http://localhost:4318", disable_batch=True)
```

For the handful of signals that never appear on any OTel span at all — pre-action governance
gates, sub-agent handoffs, non-LLM tool calls — see the separate `acp-signals` package instead of
trying to get them from tracing.
