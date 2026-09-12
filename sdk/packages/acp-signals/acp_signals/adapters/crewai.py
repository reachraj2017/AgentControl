"""
acp_signals.adapters.crewai
~~~~~~~~~~~~~~~~~~~~~~~~~~~~

** CONFIDENCE: verified against the actual latest published wheel,
``crewai==1.15.21`` (downloaded and inspected directly — the interpreter
available in this environment could only install crewai's Python-3.14-
compatible fallback, 0.11.2, so the real current release was fetched and
read separately to confirm this). ``Crew(step_callback=..., task_callback=...)``
constructor parameters still exist unchanged in 1.15.21. ``step_callback``
receives an ``AgentAction`` (fields: ``tool``, ``tool_input``, ``result`` —
matches this adapter's ``getattr`` calls exactly) or an ``AgentFinish`` (no
``tool`` field, correctly skipped by the early ``if not tool_name: return``).
``task_callback`` receives a ``TaskOutput`` whose ``agent`` field is now a
plain ``str`` (changed from an object in older releases) — this adapter's
``getattr(task_output.agent, "role", None) or str(task_output.agent)``
fallback chain still resolves correctly either way, so no change was needed.

Newer CrewAI versions have also added an event-bus system
(``crewai.utilities.events``) as an alternative first-class extension point.
If you prefer it, wire these calls to that instead — ``step_callback``/
``task_callback`` remain valid but are not the only option.

Usage::

    from crewai import Crew
    from acp_signals.adapters.crewai import acp_step_callback, acp_task_callback

    crew = Crew(
        agents=[...],
        tasks=[...],
        step_callback=acp_step_callback,
        task_callback=acp_task_callback,
    )
"""

from __future__ import annotations

import time
from typing import Any

from acp_signals.client import handoff, tool_span

_last_agent_name: str = ""
_tool_start_time: float = 0.0


def acp_step_callback(step_output: Any) -> None:
    """Register as `step_callback`. Emits a tool_span when the step used a tool."""
    global _tool_start_time
    tool_name = getattr(step_output, "tool", None) or getattr(step_output, "tool_name", None)
    if not tool_name:
        return
    now = time.time()
    latency_ms = (now - _tool_start_time) * 1000 if _tool_start_time else 0.0
    _tool_start_time = now
    tool_span(
        tool_name=str(tool_name),
        input=getattr(step_output, "tool_input", None),
        output=str(getattr(step_output, "result", ""))[:4096],
        status="ok",
        latency_ms=latency_ms,
        agent_role=_last_agent_name,
    )


def acp_task_callback(task_output: Any) -> None:
    """Register as `task_callback`. Infers a handoff when the acting agent changes."""
    global _last_agent_name
    agent_name = getattr(getattr(task_output, "agent", None), "role", None) or str(
        getattr(task_output, "agent", "")
    )
    if _last_agent_name and agent_name and _last_agent_name != agent_name:
        handoff(from_agent=_last_agent_name, to_agent=agent_name)
    if agent_name:
        _last_agent_name = agent_name
