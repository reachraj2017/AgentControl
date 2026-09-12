"""
Multi-agent system — orchestrator + two specialist agents all routed through ACP.

Shows:
  - Each agent has its own ACPClient with its own agent_role
  - Gateway routes each role to the right model/pool via traffic policies
  - Every call is traced and evaluated by M1
  - M2 governance gates the orchestrator's final action

Run:
    pip install "acp-sdk[openai,otel]"
    python examples/multi_agent_system.py
"""

import os
from acp_sdk import ACPClient

GATEWAY = os.environ.get("ACP_GATEWAY_URL", "http://localhost:8080")
EVAL    = os.environ.get("ACP_EVAL_RUNNER_URL", "http://localhost:8000")
GOV     = os.environ.get("ACP_GOVERNANCE_URL", "http://localhost:8002")
INTEL   = os.environ.get("ACP_EVALGOV_URL", "http://localhost:8003")
KEY     = os.environ.get("ACP_GATEWAY_KEY", "")
SYS     = "acp-sdk-multi-agent-demo"


def make_client(agent_name: str, agent_role: str) -> ACPClient:
    return ACPClient(
        gateway_url=GATEWAY,
        eval_runner_url=EVAL,
        governance_url=GOV,
        evalgov_url=INTEL,
        api_key=KEY,
        agent_name=agent_name,
        agent_role=agent_role,
        system_id=SYS,
    )


orchestrator = make_client("orchestrator", "orchestrator")
searcher     = make_client("searcher",     "searcher")
summarizer   = make_client("summarizer",   "summarizer")

TOPIC = "Recent advances in retrieval-augmented generation"

# ── Step 1: Searcher finds raw information ─────────────────────────────────
print("Searcher querying...")
with searcher.tracer.span(model="gpt-4o-mini") as ctx:
    ctx.prompt = f"Find key facts about: {TOPIC}"
    client = searcher.gateway.openai_client()
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": ctx.prompt}],
        max_tokens=400,
    )
    ctx.completion = r.choices[0].message.content
    ctx.tokens_in  = r.usage.prompt_tokens
    ctx.tokens_out = r.usage.completion_tokens

raw_facts = ctx.completion
print(f"  Searcher span: {ctx.span_id}")

# ── Step 2: Summarizer condenses the result ────────────────────────────────
print("Summarizer condensing...")
with summarizer.tracer.span(model="gpt-4o-mini") as ctx:
    ctx.prompt = f"Summarize in 3 bullet points:\n{raw_facts}"
    client = summarizer.gateway.openai_client()
    r = client.chat.completions.create(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": ctx.prompt}],
        max_tokens=200,
    )
    ctx.completion = r.choices[0].message.content
    ctx.tokens_in  = r.usage.prompt_tokens
    ctx.tokens_out = r.usage.completion_tokens

summary = ctx.completion
print(f"  Summarizer span: {ctx.span_id}")

# ── Step 3: Orchestrator gates publishing behind a governance check ─────────
print("Orchestrator checking governance before publishing...")
decision = orchestrator.governance.check_policy(
    action="publish_summary",
    context={"destination": "internal-wiki", "topic": TOPIC, "content": summary},
)
print(f"  Governance decision: {decision.get('decision', 'allow')}")

if decision.get("decision") == "block":
    print("  Publication blocked:", decision.get("reason"))
else:
    print("\nFinal summary:\n")
    print(summary)
    print("\nAll three spans are in ClickHouse and have been evaluated by M1.")
    print("Query them via EvalGov:")
    answer = orchestrator.intelligence.chat(
        f"What eval scores did the searcher and summarizer agents get in the last 5 minutes?"
    )
    print(answer)
