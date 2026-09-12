"""
CrewAI integration — run a CrewAI crew with all LLM calls routed through ACP.

Every agent in the crew uses the ACP gateway as its LLM backend. All calls
are evaluated by M1, governed by M2, and queryable via EvalGov (M4).

Run:
    pip install "acp-sdk[openai,otel]" crewai
    python examples/crewai_integration.py
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
    agent_name="crewai-crew",
    agent_role="crewai",
    system_id="acp-sdk-examples",
)

try:
    from crewai import Agent, Task, Crew
    from langchain_openai import ChatOpenAI
except ImportError:
    print("Install crewai to run this example:")
    print("  pip install crewai langchain-openai")
    raise

# Both agents share the same gateway-backed LLM.
# Each call is traced separately because the ACP gateway generates a unique
# span ID per request from the X-Gateway-Agent-Role and conversation context.
gateway_llm = ChatOpenAI(
    model="gpt-4o-mini",
    openai_api_key=KEY or "not-required",
    openai_api_base=f"{GATEWAY}/v1",
    default_headers=acp.gateway._role_headers(),
)

researcher = Agent(
    role="Researcher",
    goal="Find concise facts on a given topic",
    backstory="Expert at rapid factual research",
    llm=gateway_llm,
    verbose=False,
)

writer = Agent(
    role="Writer",
    goal="Turn raw facts into a polished one-paragraph summary",
    backstory="Clear, concise technical writer",
    llm=gateway_llm,
    verbose=False,
)

topic = "OpenTelemetry for LLM observability"

research_task = Task(
    description=f"Research: {topic}. Return 5 key bullet points.",
    expected_output="5 bullet points",
    agent=researcher,
)

write_task = Task(
    description="Turn the research into a polished one-paragraph summary for a developer audience.",
    expected_output="One paragraph summary",
    agent=writer,
)

crew = Crew(agents=[researcher, writer], tasks=[research_task, write_task], verbose=False)
result = crew.kickoff()
print(result)
print("\nAll CrewAI LLM calls routed through M3, evaluated by M1, governed by M2.")
