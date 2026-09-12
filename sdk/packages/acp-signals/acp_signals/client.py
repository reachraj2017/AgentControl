"""
acp_signals.client
~~~~~~~~~~~~~~~~~~~
Explicit client for the three signal types the gateway wire protocol can never
see: pre-action governance checkpoints, sub-agent handoffs, and non-LLM tool
executions. Each is a small, stable POST to the AI Control Plane Agent
Gateway (M3) — the same front door as LLM traffic, not a separate side channel.

These are explicit, developer-added calls, not passive instrumentation. That
is a deliberate design choice: passive interception (patching a framework's
client, registering a competing global tracer) is fragile against a
framework's own internals and its own telemetry in a way an explicit,
stable function call at a point the developer already controls is not.

Usage::

    from acp_signals import context, checkpoint, handoff, tool_span

    context.set(conversation_id="conv-1", system_id="my-product", agent_role="orchestrator")

    decision = checkpoint("send_email", risk_level="high", metadata={"to": "user@example.com"})
    if decision.decision == "block":
        raise PermissionError(decision.reason)

    handoff("orchestrator", "summarizer", context_summary="user asked for a 30-word summary")

    tool_span("web_search", input={"query": "quantum computing"}, output={"hits": 5}, latency_ms=210)
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from acp_signals import context as _context

_SCHEMA_VERSION = "1.0"

# Fire-and-forget executor for handoff()/tool_span() — small, bounded, daemon-like.
# Mirrors the gateway's own `_emit_async` pattern: never block the caller's
# execution path on a structural/telemetry POST.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="acp-signals")


@dataclass(frozen=True)
class Decision:
    """Result of a ``checkpoint()`` call."""

    decision: str  # "allow" | "block" | "hitl_pending"
    checkpoint_id: str = ""
    reason: str = ""
    error: str = ""

    @property
    def allowed(self) -> bool:
        return self.decision == "allow"


class SignalsClient:
    """
    Client for the gateway's structural/governance signal endpoints
    (``/v1/checkpoint``, ``/v1/handoff``, ``/v1/tool-span``).

    Args:
        gateway_url: Base URL of the ACP Agent Gateway (e.g. ``http://localhost:8080``).
                     Defaults to the ``ACP_GATEWAY_URL`` env var, then ``http://localhost:8080``
                     — matching the convention used by ``acp_gateway.GatewayClient`` and
                     ``opt-demo/acp_setup.py``.
        api_key:     Virtual gateway API key (``gw-sk-*``). Defaults to ``GATEWAY_API_KEY`` env var.
    """

    def __init__(self, gateway_url: str = "", api_key: str = "") -> None:
        self._gateway_url = (gateway_url or os.getenv("ACP_GATEWAY_URL", "http://localhost:8080")).rstrip("/")
        self._api_key = api_key or os.getenv("GATEWAY_API_KEY", "")

    # ── Checkpoint (blocking gate) ───────────────────────────────────────────────

    def checkpoint(
        self,
        action: str,
        risk_level: str = "medium",
        metadata: Optional[dict[str, Any]] = None,
        *,
        conversation_id: Optional[str] = None,
        system_id: Optional[str] = None,
        agent_role: Optional[str] = None,
        timeout: float = 30.0,
    ) -> Decision:
        """
        Synchronous pre-action gate. Blocks until the gateway (forwarding to the
        M2 governance-service) returns a decision.

        Identity fields default to the current :mod:`acp_signals.context` unless
        explicitly overridden.
        """
        ctx = _context.get()
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "action": action,
            "risk_level": risk_level,
            "system_id": system_id if system_id is not None else ctx.system_id,
            "agent_role": agent_role if agent_role is not None else ctx.agent_role,
            "conversation_id": conversation_id if conversation_id is not None else ctx.conversation_id,
            "metadata": metadata or {},
        }
        try:
            with httpx.Client(timeout=timeout) as c:
                r = c.post(f"{self._gateway_url}/v1/checkpoint", json=payload, headers=self._headers())
                r.raise_for_status()
                data = r.json()
            return Decision(
                decision=data.get("decision", "allow"),
                checkpoint_id=data.get("checkpoint_id", ""),
                reason=data.get("reason", ""),
            )
        except Exception as exc:
            # Fail-open by default, matching the gateway's documented fail-open
            # enforcement posture. Callers that need fail-closed behavior for a
            # specific action should treat Decision.error != "" as a hard stop.
            return Decision(decision="allow", error=str(exc))

    # ── Handoff (fire-and-forget) ────────────────────────────────────────────────

    def handoff(
        self,
        from_agent: str,
        to_agent: str,
        context_summary: str = "",
        *,
        conversation_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """Record a sub-agent handoff. Does not block the caller."""
        ctx = _context.get()
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "from_agent": from_agent,
            "to_agent": to_agent,
            "conversation_id": conversation_id if conversation_id is not None else ctx.conversation_id,
            "run_id": run_id if run_id is not None else ctx.run_id,
            "context_summary": context_summary,
        }
        _executor.submit(self._post_fire_and_forget, "/v1/handoff", payload)

    # ── Tool span (fire-and-forget) ──────────────────────────────────────────────

    def tool_span(
        self,
        tool_name: str,
        input: Any = None,  # noqa: A002 - matches the design doc's field name
        output: Any = None,
        status: str = "ok",
        latency_ms: float = 0.0,
        *,
        agent_role: Optional[str] = None,
        conversation_id: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> None:
        """Record a non-LLM tool execution. Does not block the caller."""
        ctx = _context.get()
        payload = {
            "schema_version": _SCHEMA_VERSION,
            "tool_name": tool_name,
            "input": input,
            "output": output,
            "status": status,
            "latency_ms": latency_ms,
            "agent_role": agent_role if agent_role is not None else ctx.agent_role,
            "conversation_id": conversation_id if conversation_id is not None else ctx.conversation_id,
            "run_id": run_id if run_id is not None else ctx.run_id,
        }
        _executor.submit(self._post_fire_and_forget, "/v1/tool-span", payload)

    # ── Internal ──────────────────────────────────────────────────────────────────

    def _post_fire_and_forget(self, path: str, payload: dict) -> None:
        try:
            with httpx.Client(timeout=10.0) as c:
                c.post(f"{self._gateway_url}{path}", json=payload, headers=self._headers())
        except Exception:
            pass  # structural signals must never crash or block the host application

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self._api_key:
            h["Authorization"] = f"Bearer {self._api_key}"
        return h

    def __repr__(self) -> str:
        return f"SignalsClient(gateway_url={self._gateway_url!r})"


# ── Module-level default client + convenience functions ────────────────────────
# Mirrors the ergonomics of a typical logging/tracing library: import the
# function directly, no client object required for the common case. A custom
# client (different gateway URL/key) can still be built explicitly via
# SignalsClient(...) and its methods called directly.

_default_client: Optional[SignalsClient] = None


def _client() -> SignalsClient:
    global _default_client
    if _default_client is None:
        _default_client = SignalsClient()
    return _default_client


def configure(gateway_url: str = "", api_key: str = "") -> SignalsClient:
    """Replace the module-level default client (e.g. to point at a non-default gateway)."""
    global _default_client
    _default_client = SignalsClient(gateway_url=gateway_url, api_key=api_key)
    return _default_client


def checkpoint(action: str, risk_level: str = "medium", metadata: Optional[dict[str, Any]] = None, **kwargs: Any) -> Decision:
    return _client().checkpoint(action, risk_level=risk_level, metadata=metadata, **kwargs)


def handoff(from_agent: str, to_agent: str, context_summary: str = "", **kwargs: Any) -> None:
    _client().handoff(from_agent, to_agent, context_summary=context_summary, **kwargs)


def tool_span(
    tool_name: str,
    input: Any = None,  # noqa: A002
    output: Any = None,
    status: str = "ok",
    latency_ms: float = 0.0,
    **kwargs: Any,
) -> None:
    _client().tool_span(tool_name, input=input, output=output, status=status, latency_ms=latency_ms, **kwargs)


def _timed_tool_span_start() -> float:
    """Helper for adapters: call at tool-start, pass the result to `_timed_tool_span_end`."""
    return time.time()
