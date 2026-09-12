"""ACP SDK setup — wires M1 (tracing), M2 (governance), M3 (gateway) into opt-demo."""
import os
import sys

# Add the top-level SDK packages to path so they can be imported without a pip install.
_repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_sdk_root = os.path.join(_repo_root, "sdk", "packages")
for _pkg in ("acp-tracing", "acp-governance", "acp-gateway"):
    _pkg_path = os.path.join(_sdk_root, _pkg)
    if _pkg_path not in sys.path:
        sys.path.insert(0, _pkg_path)

from acp_tracing import instrument, get_tracer
from acp_governance import GovernanceClient
from acp_gateway import GatewayClient

_OTLP_ENDPOINT  = os.getenv("ACP_OTEL_ENDPOINT",    "http://localhost:4318")
_GOV_URL        = os.getenv("ACP_GOVERNANCE_URL",    "http://localhost:8002")
_GATEWAY_URL    = os.getenv("ACP_GATEWAY_URL",        "http://localhost:8080")
_GATEWAY_KEY    = os.getenv("GATEWAY_API_KEY",        "")

# M1: set up a global OTel TracerProvider that exports to the ACP eval-runner.
# ADK automatically attaches its spans to this provider.
instrument("opt-demo", agent_role="orchestrator", otlp_endpoint=_OTLP_ENDPOINT)
tracer = get_tracer("opt-demo")

# Activate OpenAI SDK instrumentation so every LiteLlm call automatically injects
# a W3C traceparent header — the gateway reads this and stores it as trace_id,
# enabling prompt_evals ↔ gateway_call_log correlation.
try:
    from opentelemetry.instrumentation.openai import OpenAIInstrumentor
    OpenAIInstrumentor().instrument()
except ImportError:
    pass

# M2: governance client (policy enforcement, circuit breakers, trust scores)
gov = GovernanceClient(
    governance_url=_GOV_URL,
    agent_name="opt-demo",
    system_id="opt-demo",
)

# M3: configure OPENAI_BASE_URL so LiteLlm in ADK routes through the gateway
gw = GatewayClient(
    gateway_url=_GATEWAY_URL,
    api_key=_GATEWAY_KEY,
    agent_role="orchestrator",
    system_id="opt-demo",
)
os.environ["OPENAI_BASE_URL"] = f"{_GATEWAY_URL}/v1"

# Inject governance client into agents module so _before_tool_cb can use it
import agents.orchestrator as _orch_module
_orch_module.set_governance_client(gov)


def get_acp_tracer():
    return tracer


def get_gov_client():
    return gov


def get_gateway_client():
    return gw
