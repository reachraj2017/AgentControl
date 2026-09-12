"""
Custom / raw agent loop — checkpoint, handoff, tool_span with no framework.

This is the "no framework at all" row from design/checkpoint-handoff-ingest.md
§4: no adapter exists (or is needed) because there's no framework-owned hook
to attach to — you call the three explicit signal functions directly at the
points in your own code where they apply. This is the same small, stable API
surface a framework adapter uses internally; a framework just calls it from
inside a callback instead of a plain function body.

Run against a live ACP stack:
    pip install acp-signals
    python examples/custom_agent_signals.py
"""

import os

from acp_signals import checkpoint, context, handoff, tool_span

context.set(
    conversation_id="demo-conv-1",
    system_id="acp-sdk-examples",
    agent_role="orchestrator",
)


def web_search(query: str) -> dict:
    """Pretend tool call — in a real agent this hits a search API."""
    return {"hits": 3, "top_result": f"result for: {query}"}


def send_email(to: str, body: str) -> None:
    """Pretend irreversible action — gated behind a checkpoint below."""
    print(f"[would send email to {to}]: {body}")


def run_custom_agent_loop(user_message: str) -> str:
    # Step 1 — orchestrator does a tool call.
    import time

    start = time.time()
    result = web_search(user_message)
    tool_span(
        "web_search",
        input={"query": user_message},
        output=result,
        status="ok",
        latency_ms=(time.time() - start) * 1000,
    )

    # Step 2 — hand off to a "summarizer" sub-agent (no LLM framework here,
    # just a plain function call standing in for one).
    handoff("orchestrator", "summarizer", context_summary=f"summarize: {result}")
    context.set(agent_role="summarizer")
    summary = f"Found {result['hits']} results for '{user_message}'."

    # Step 3 — an irreversible action gated behind a checkpoint.
    decision = checkpoint(
        "send_email",
        risk_level="high",
        metadata={"to": "user@example.com", "summary_len": len(summary)},
    )
    if decision.decision == "block":
        print(f"Blocked: {decision.reason}")
        return summary
    if decision.decision == "hitl_pending":
        print(f"Pending human approval: checkpoint_id={decision.checkpoint_id}")
        return summary

    send_email("user@example.com", summary)
    return summary


if __name__ == "__main__":
    os.environ.setdefault("ACP_GATEWAY_URL", "http://localhost:8080")
    output = run_custom_agent_loop("quantum computing breakthroughs")
    print(output)
