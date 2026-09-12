"""
acp-sdk — AI Control Plane umbrella SDK.

Consolidates all four module SDKs into a single install:

    pip install acp-sdk

Usage::

    from acp_sdk import ACPClient
    from acp_sdk import GatewayClient, ACPTracer, GovernanceClient, IntelligenceClient

The :class:`ACPClient` is a thin composition layer that wires all four
module clients to a shared configuration. Use it when you want a single
object to interact with the entire control plane.

For selective installs (when not all modules are deployed), install the
individual packages instead::

    pip install acp-gateway
    pip install acp-tracing
    pip install acp-governance
    pip install acp-intelligence
"""

from acp_sdk.client import ACPClient

# Re-export individual clients for direct access
try:
    from acp_gateway import GatewayClient
except ImportError:
    GatewayClient = None  # type: ignore[assignment,misc]

try:
    from acp_tracing import ACPTracer
except ImportError:
    ACPTracer = None  # type: ignore[assignment,misc]

try:
    from acp_governance import GovernanceClient
except ImportError:
    GovernanceClient = None  # type: ignore[assignment,misc]

try:
    from acp_intelligence import IntelligenceClient
except ImportError:
    IntelligenceClient = None  # type: ignore[assignment,misc]

try:
    from acp_signals import SignalsClient
except ImportError:
    SignalsClient = None  # type: ignore[assignment,misc]

__all__ = [
    "ACPClient",
    "GatewayClient",
    "ACPTracer",
    "GovernanceClient",
    "IntelligenceClient",
    "SignalsClient",
]
__version__ = "0.1.0"
