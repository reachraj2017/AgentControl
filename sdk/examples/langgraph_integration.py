"""
LangGraph integration — wire a LangGraph ReAct agent through the ACP gateway.

Every LangGraph node call is routed through M3 for policy enforcement, and
each tool use is traced to M1 for automatic evaluation.

Run:
    pip install "acp-sdk[openai,otel]" langgraph langchain-openai
    python examples/langgraph_integration.py
"""

import os
from acp_sdk import ACPClient

GATEWAY = os.environ.get("ACP_GATEWAY_URL", "http://localhost:8080")
KEY     = os.environ.get("ACP_GATEWAY_KEY", "")

acp = ACPClient(
    gateway_url=GATEWAY,
    eval_runner_url=os.environ.get("ACP_EVAL_RUNNER_URL", "http://localhost:8000"),
    governance_url=os.environ.get("ACP_GOVERNANCE_URL", "http://localhost:8002"),
    evalgov_url=os.environ.get("ACP_EVALGOV_URL", "http://localhost:8003"),
    api_key=KEY,
    agent_name="langgraph-agent",
    agent_role="langgraph",
    system_id="acp-sdk-examples",
)

try:
    from langgraph.graph import StateGraph, END
    from langchain_openai import ChatOpenAI
    from langchain_core.messages import HumanMessage
except ImportError:
    print("Install langgraph and langchain-openai to run this example:")
    print("  pip install langgraph langchain-openai")
    raise

# Point LangChain's ChatOpenAI at the ACP gateway.
# The gateway transparently proxies to the real OpenAI endpoint while adding
# routing, caching, and governance enforcement.
llm = ChatOpenAI(
    model="gpt-4o-mini",
    openai_api_key=KEY or "not-required",
    openai_api_base=f"{GATEWAY}/v1",
    default_headers=acp.gateway._role_headers(),  # injects agent_role + system_id
)


def call_model(state):
    with acp.tracer.span(model="gpt-4o-mini") as ctx:
        ctx.prompt = state["messages"][-1].content
        response = llm.invoke(state["messages"])
        ctx.completion = response.content
    return {"messages": state["messages"] + [response]}


# Minimal single-node graph for illustration
graph = StateGraph(dict)
graph.add_node("agent", call_model)
graph.set_entry_point("agent")
graph.add_edge("agent", END)
app = graph.compile()

result = app.invoke({"messages": [HumanMessage(content="What is 2 + 2?")]})
print(result["messages"][-1].content)
print("\nAll LangGraph LLM calls are now traced to M1 and governed by M2.")
