"""
Basic usage — send one LLM call through the gateway and trace it.

Run against a live ACP stack:
    pip install "acp-sdk[openai,otel]"
    python examples/basic_usage.py
"""

import os
from acp_sdk import ACPClient

acp = ACPClient(
    gateway_url=os.environ.get("ACP_GATEWAY_URL", "http://localhost:8080"),
    eval_runner_url=os.environ.get("ACP_EVAL_RUNNER_URL", "http://localhost:8000"),
    governance_url=os.environ.get("ACP_GOVERNANCE_URL", "http://localhost:8002"),
    evalgov_url=os.environ.get("ACP_EVALGOV_URL", "http://localhost:8003"),
    api_key=os.environ.get("ACP_GATEWAY_KEY", ""),
    agent_name="basic-example-agent",
    agent_role="demo",
    system_id="acp-sdk-examples",
)

# Check connectivity
status = acp.health()
print("Health:", status)

USER_PROMPT = "Summarize the role of observability in production AI systems in two sentences."

# Option A — use the gateway directly via GatewayClient
print("\n--- Direct gateway call ---")
resp = acp.gateway.chat_openai(
    model="gpt-4o-mini",
    messages=[{"role": "user", "content": USER_PROMPT}],
)
if "error" not in resp:
    print(resp["choices"][0]["message"]["content"])
else:
    print("Error:", resp["error"])

# Option B — use the gateway as an OpenAI drop-in and instrument the call with M1 tracing
print("\n--- Traced gateway call ---")
client = acp.gateway.openai_client()

with acp.tracer.span(model="gpt-4o-mini") as ctx:
    ctx.prompt = USER_PROMPT
    api_resp = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": USER_PROMPT}],
        max_tokens=200,
    )
    ctx.completion = api_resp.choices[0].message.content
    ctx.tokens_in = api_resp.usage.prompt_tokens
    ctx.tokens_out = api_resp.usage.completion_tokens

print(ctx.completion)
print(f"\nSpan: {ctx.span_id}  tokens: {ctx.tokens_in}→{ctx.tokens_out}  latency: {ctx.latency_ms:.0f}ms")
print("The span was exported to M1 and will be evaluated automatically.")
