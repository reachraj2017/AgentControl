"""
acp_signals.adapters.openai_agents
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

** CONFIDENCE: verified against installed ``openai-agents==0.22.2`` source
(``agents/lifecycle.py``, ``RunHooksBase``). Every method below matches the
real signature exactly:

    on_handoff(context, from_agent, to_agent)
    on_tool_start(context, agent, tool)
    on_tool_end(context, agent, tool, result)

The openai-agents SDK is young and has changed hook signatures before —
re-verify against ``agents.lifecycle`` if you're on a materially different
version. If a method signature has changed, only this one file needs to
change — the ``acp_signals.handoff()`` / ``tool_span()`` calls it makes are
stable.

Usage::

    from agents import Runner
    from acp_signals.adapters.openai_agents import ACPRunHooks

    result = await Runner.run(
        starting_agent,
        input=user_message,
        hooks=ACPRunHooks(),
    )
"""

from __future__ import annotations

import time
from typing import Any

from acp_signals.client import handoff, tool_span

try:
    from agents import RunHooks  # type: ignore[import-not-found]
    _AGENTS_AVAILABLE = True
except ImportError:
    RunHooks = object  # type: ignore[assignment,misc]
    _AGENTS_AVAILABLE = False


class ACPRunHooks(RunHooks):  # type: ignore[misc,valid-type]
    """Translates openai-agents SDK lifecycle events into ACP signal calls."""

    def __init__(self) -> None:
        if not _AGENTS_AVAILABLE:
            raise ImportError(
                "The 'openai-agents' package is required for this adapter: "
                "pip install openai-agents"
            )
        super().__init__()
        self._tool_starts: dict[str, float] = {}

    async def on_handoff(self, context: Any, from_agent: Any, to_agent: Any) -> None:
        handoff(
            from_agent=getattr(from_agent, "name", str(from_agent)),
            to_agent=getattr(to_agent, "name", str(to_agent)),
        )

    async def on_tool_start(self, context: Any, agent: Any, tool: Any) -> None:
        tool_name = getattr(tool, "name", str(tool))
        self._tool_starts[tool_name] = time.time()

    async def on_tool_end(self, context: Any, agent: Any, tool: Any, result: Any) -> None:
        tool_name = getattr(tool, "name", str(tool))
        started = self._tool_starts.pop(tool_name, None)
        latency_ms = (time.time() - started) * 1000 if started else 0.0
        tool_span(
            tool_name=tool_name,
            input=getattr(tool, "params_json_schema", None) or {},
            output=str(result)[:4096],
            status="ok",
            latency_ms=latency_ms,
            agent_role=getattr(agent, "name", ""),
        )
