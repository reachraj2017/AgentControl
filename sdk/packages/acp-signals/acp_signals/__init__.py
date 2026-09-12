"""
acp-signals — explicit checkpoint / handoff / tool-span client for the AI Control Plane.

Covers the signals that never cross the LLM wire and so can never be captured
by gateway wire capture or by passive OTel instrumentation: pre-action
governance gates, sub-agent handoffs, and non-LLM tool executions.
"""

from acp_signals import adapters, context
from acp_signals.client import Decision, SignalsClient, checkpoint, configure, handoff, tool_span

__all__ = [
    "SignalsClient",
    "Decision",
    "checkpoint",
    "handoff",
    "tool_span",
    "configure",
    "context",
    "adapters",
]
__version__ = "0.1.0"
