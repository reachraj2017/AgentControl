# Examples

| File | What it demonstrates |
|---|---|
| `basic_usage.py` | Single LLM call through the gateway + OTLP tracing to M1 |
| `multi_agent_system.py` | Orchestrator + two specialist agents each with their own `agent_role`, plus governance gating |
| `langgraph_integration.py` | LangGraph ReAct agent with gateway-backed ChatOpenAI |
| `crewai_integration.py` | CrewAI crew with all agents using the gateway as the LLM backend |

## Prerequisites

```bash
# Start the ACP stack (from the repo root)
cd /path/to/this/repo
make up

# Install the SDK packages (from the repo root — they live in sdk/packages/ here)
pip install -e "sdk/packages/acp-gateway[all]"
pip install -e "sdk/packages/acp-tracing[otel]"
pip install -e "sdk/packages/acp-governance"
pip install -e "sdk/packages/acp-signals"
pip install -e "sdk/packages/acp-intelligence"
pip install -e "sdk/packages/acp-sdk"
```

## Environment variables

```bash
export ACP_GATEWAY_URL="http://localhost:8080"
export ACP_EVAL_RUNNER_URL="http://localhost:8000"
export ACP_GOVERNANCE_URL="http://localhost:8002"
export ACP_EVALGOV_URL="http://localhost:8003"
export ACP_GATEWAY_KEY="gw-sk-..."   # from the portal → API Keys
```

## Run

```bash
python examples/basic_usage.py
python examples/multi_agent_system.py
python examples/langgraph_integration.py
python examples/crewai_integration.py
```
