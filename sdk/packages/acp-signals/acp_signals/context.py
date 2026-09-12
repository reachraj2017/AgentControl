"""
acp_signals.context
~~~~~~~~~~~~~~~~~~~~
Lightweight, explicit correlation context — a ``contextvars.ContextVar`` holder
for the identity fields that ``checkpoint()`` / ``handoff()`` / ``tool_span()``
attach to every call: ``conversation_id``, ``run_id``, ``system_id``, ``agent_role``.

This intentionally is NOT a global OTel TracerProvider. Registering a global
tracer competes with a framework's own tracing (this is exactly what broke in
production against the openai-agents SDK — see
docs/external-agent-integration-findings.md, Issue 5). A plain contextvar has
no such conflict: it's local state the bootstrap sets once per request/turn,
and adapters or application code can override at finer granularity (e.g. a
framework's ``on_handoff`` hook re-setting ``agent_role`` when the active
agent changes).

Usage::

    from acp_signals import context

    context.set(conversation_id="conv-123", system_id="my-product", agent_role="orchestrator")
    ...
    # deeper in the call stack, no need to thread the ids through every function
    context.set(agent_role="summarizer")   # partial update — other fields unchanged
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass, replace
from typing import Optional


@dataclass(frozen=True)
class SignalContext:
    conversation_id: str = ""
    run_id: str = ""
    system_id: str = ""
    agent_role: str = ""


_current: ContextVar[SignalContext] = ContextVar("acp_signal_context", default=SignalContext())


def set(
    conversation_id: Optional[str] = None,
    run_id: Optional[str] = None,
    system_id: Optional[str] = None,
    agent_role: Optional[str] = None,
) -> SignalContext:
    """
    Update the current context. Any field left as ``None`` keeps its prior value
    (partial update) — so a handoff hook can call ``context.set(agent_role="x")``
    without needing to know the current conversation/run id.

    Returns the resulting :class:`SignalContext`.
    """
    prior = _current.get()
    updates = {}
    if conversation_id is not None:
        updates["conversation_id"] = conversation_id
    if run_id is not None:
        updates["run_id"] = run_id
    if system_id is not None:
        updates["system_id"] = system_id
    if agent_role is not None:
        updates["agent_role"] = agent_role
    new_ctx = replace(prior, **updates)
    _current.set(new_ctx)
    return new_ctx


def get() -> SignalContext:
    """Return the current :class:`SignalContext` (all-empty-string defaults if unset)."""
    return _current.get()


def reset() -> None:
    """Reset the context to all-empty-string defaults. Mainly useful in tests."""
    _current.set(SignalContext())
