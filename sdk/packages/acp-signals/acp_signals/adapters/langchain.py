"""
acp_signals.adapters.langchain
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

** CONFIDENCE: verified against installed ``langchain-core==1.6.2`` source.
``BaseCallbackHandler`` is a long-stable, widely-used public LangChain
extension point. Confirmed by reading ``langchain_core/callbacks/base.py``
directly: ``on_tool_end``/``on_tool_error`` do NOT receive a ``name`` or
``inputs`` kwarg (an earlier draft of this adapter assumed they did — that
was wrong and silently produced a placeholder ``tool_name="tool"`` for every
span). The tool name and input only arrive in ``on_tool_start``'s own
params (``serialized['name']``, ``input_str``) and must be captured there,
keyed by ``run_id``, for lookup in the paired end/error callback — which is
what this version does. Re-verify against future ``langchain-core`` majors
if this stops matching. **

Tool-span coverage (``on_tool_start`` / ``on_tool_end`` / ``on_tool_error``) is
solid for any LangChain or LangGraph app. Handoff inference for LangGraph
multi-agent graphs is best-effort: LangChain's callback system has no single
first-class "handoff" event, so this adapter infers a handoff whenever
``on_chain_start`` fires for a chain/node whose name differs from the
previously-active one — a role-change heuristic, consistent with the same
approach used in ``design/v2-gateway-capture-m1-ingest.md`` §5.4 and in the
``google_adk`` adapter. If your graph names every node distinctly, this is a
reasonable proxy for agent boundaries; if it doesn't, handoff events may be
noisy or absent.

Usage::

    from acp_signals.adapters.langchain import ACPCallbackHandler

    chain.invoke(input, config={"callbacks": [ACPCallbackHandler()]})
"""

from __future__ import annotations

import time
from typing import Any
from uuid import UUID

from acp_signals.client import handoff, tool_span

try:
    from langchain_core.callbacks import BaseCallbackHandler
    _LANGCHAIN_AVAILABLE = True
except ImportError:
    BaseCallbackHandler = object  # type: ignore[assignment,misc]
    _LANGCHAIN_AVAILABLE = False


class ACPCallbackHandler(BaseCallbackHandler):  # type: ignore[misc,valid-type]
    """Translates LangChain/LangGraph callback events into ACP signal calls."""

    def __init__(self) -> None:
        if not _LANGCHAIN_AVAILABLE:
            raise ImportError(
                "The 'langchain-core' package is required for this adapter: "
                "pip install langchain-core"
            )
        super().__init__()
        # Keyed by run_id: (start_time, tool_name, input). langchain_core's real
        # on_tool_end/on_tool_error signatures (verified against langchain-core
        # 1.6.2) do NOT receive a 'name' or 'inputs' kwarg — those only arrive
        # in on_tool_start's own params (serialized['name'], input_str). Must
        # capture them there and look them up by run_id, not read them off
        # on_tool_end's kwargs (which was the original, incorrect assumption).
        self._tool_starts: dict[str, tuple[float, str, str]] = {}
        self._last_chain_name: str = ""

    # ── Tool spans ────────────────────────────────────────────────────────────

    def on_tool_start(
        self,
        serialized: dict[str, Any],
        input_str: str,
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        tool_name = (serialized or {}).get("name", "") or kwargs.get("name", "tool")
        self._tool_starts[str(run_id)] = (time.time(), tool_name, input_str)

    def on_tool_end(self, output: Any, *, run_id: UUID, **kwargs: Any) -> None:
        started, tool_name, tool_input = self._tool_starts.pop(
            str(run_id), (None, "tool", None)
        )
        latency_ms = (time.time() - started) * 1000 if started else 0.0
        tool_span(
            tool_name=tool_name,
            input=tool_input,
            output=str(output)[:4096],
            status="ok",
            latency_ms=latency_ms,
        )

    def on_tool_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        started, tool_name, tool_input = self._tool_starts.pop(
            str(run_id), (None, "tool", None)
        )
        latency_ms = (time.time() - started) * 1000 if started else 0.0
        tool_span(
            tool_name=tool_name,
            input=tool_input,
            output=str(error)[:4096],
            status="error",
            latency_ms=latency_ms,
        )

    # ── Handoff inference (best-effort — see module docstring) ──────────────────

    def on_chain_start(
        self,
        serialized: dict[str, Any],
        inputs: dict[str, Any],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        chain_name = (serialized or {}).get("name", "") or kwargs.get("name", "")
        if chain_name and self._last_chain_name and chain_name != self._last_chain_name:
            handoff(from_agent=self._last_chain_name, to_agent=chain_name)
        if chain_name:
            self._last_chain_name = chain_name
