"""
acp_signals.adapters.google_adk
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

** CONFIDENCE: verified against installed ``google-adk==2.8.0`` source
(``google/adk/agents/llm_agent.py``, ``base_agent.py`` — the
``BeforeToolCallback``/``AfterToolCallback``/``BeforeAgentCallback`` type
aliases). Matches exactly:

    BeforeAgentCallback = (callback_context: CallbackContext) -> Optional[Content]
    BeforeToolCallback  = (tool: BaseTool, args: dict, tool_context: ToolContext) -> Optional[dict]
    AfterToolCallback   = (tool: BaseTool, args: dict, tool_context: ToolContext, tool_response: dict) -> Optional[dict]

ADK is under active development — re-verify against ``google.adk.agents``
if you're on a materially different version. If a signature differs, only
this one file needs to change.

Usage::

    from google.adk.agents import Agent
    from acp_signals.adapters.google_adk import (
        acp_before_tool_callback, acp_after_tool_callback,
        acp_before_agent_callback,
    )

    agent = Agent(
        name="orchestrator",
        before_agent_callback=acp_before_agent_callback,
        before_tool_callback=acp_before_tool_callback,
        after_tool_callback=acp_after_tool_callback,
        ...
    )
"""

from __future__ import annotations

import time
from typing import Any

from acp_signals.client import handoff, tool_span

# Track the previously-active agent name (per process) to infer a handoff
# when ADK's before_agent_callback fires for a different agent than last time.
# ADK doesn't expose an explicit "handoff" event the way openai-agents does;
# this is a role-change heuristic — the same approach used in the
# ``langchain`` adapter.
_last_agent_name: str = ""

# Tool-start timestamps, keyed by (agent name, tool name) to compute latency
# in the paired after_tool_callback.
_tool_starts: dict[tuple[str, str], float] = {}


def acp_before_agent_callback(callback_context: Any, *args: Any, **kwargs: Any) -> None:
    """Register as `before_agent_callback`. Infers a handoff on agent change."""
    global _last_agent_name
    agent_name = getattr(getattr(callback_context, "agent", None), "name", "") or str(
        getattr(callback_context, "agent_name", "")
    )
    if _last_agent_name and agent_name and _last_agent_name != agent_name:
        handoff(from_agent=_last_agent_name, to_agent=agent_name)
    _last_agent_name = agent_name or _last_agent_name


def acp_before_tool_callback(tool: Any, args: dict, tool_context: Any, *_a: Any, **_k: Any) -> None:
    """Register as `before_tool_callback`. Records the start time for latency calc."""
    tool_name = getattr(tool, "name", str(tool))
    _tool_starts[(_last_agent_name, tool_name)] = time.time()


def acp_after_tool_callback(
    tool: Any, args: dict, tool_context: Any, tool_response: Any, *_a: Any, **_k: Any
) -> None:
    """Register as `after_tool_callback`. Emits the completed tool_span."""
    tool_name = getattr(tool, "name", str(tool))
    started = _tool_starts.pop((_last_agent_name, tool_name), None)
    latency_ms = (time.time() - started) * 1000 if started else 0.0
    tool_span(
        tool_name=tool_name,
        input=args,
        output=str(tool_response)[:4096],
        status="ok",
        latency_ms=latency_ms,
        agent_role=_last_agent_name,
    )
