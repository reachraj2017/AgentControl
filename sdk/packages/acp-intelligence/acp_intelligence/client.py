"""
acp_intelligence.client
~~~~~~~~~~~~~~~~~~~~~~~
Client for the AI Control Plane EvalGov Intelligence API (M4).

Query the EvalGov coordinator via natural language, retrieve proactive
findings, and check system health — all from within an external agent.
"""

from __future__ import annotations

from typing import Any

import httpx


class IntelligenceClient:
    """
    Client for the ACP EvalGov Coordinator (M4).

    Args:
        evalgov_url: Base URL of the EvalGov coordinator
                     (e.g. ``http://localhost:8003``).
        timeout:     HTTP timeout in seconds for chat calls (default 300 to
                     match the coordinator's own sub-agent call budget).

    Example::

        from acp_intelligence import IntelligenceClient

        intel = IntelligenceClient(evalgov_url="http://localhost:8003")

        # Ask EvalGov a question in natural language
        answer = intel.chat("What is the current trust score for the summarizer agent?")
        print(answer)

        # Get proactive findings
        findings = intel.get_findings()
        for f in findings:
            print(f["severity"], f["summary"])

        # Multi-turn conversation
        history = []
        r1 = intel.chat("Show me gateway error rates", history=history)
        history.append({"role": "user", "content": "Show me gateway error rates"})
        history.append({"role": "assistant", "content": r1})
        r2 = intel.chat("Why is the error rate high?", history=history)
    """

    def __init__(
        self,
        evalgov_url: str = "http://localhost:8003",
        timeout: float = 300.0,
    ) -> None:
        self._url = evalgov_url.rstrip("/")
        self._timeout = timeout

    # ── Conversational interface ───────────────────────────────────────────────

    def chat(
        self,
        message: str,
        history: list[dict] | None = None,
    ) -> str:
        """
        Send a natural-language message to the EvalGov coordinator.

        ``history`` is a list of ``{"role": "user"|"assistant", "content": str}``
        dicts representing the prior conversation turns.

        Returns the coordinator's response as a plain string.
        On error returns an error string starting with ``"[error]"``.
        """
        payload: dict[str, Any] = {"message": message}
        if history:
            payload["history"] = history
        try:
            with httpx.Client(timeout=self._timeout) as c:
                r = c.post(f"{self._url}/chat", json=payload)
                r.raise_for_status()
                data = r.json()
                return data.get("response", "")
        except Exception as exc:
            return f"[error] {exc}"

    # ── Findings ───────────────────────────────────────────────────────────────

    def get_findings(self, status: str = "active") -> list[dict]:
        """
        Return proactive findings from the EvalGov monitor.

        ``status`` is one of ``"active"``, ``"acknowledged"``, or ``"resolved"``.
        """
        try:
            with httpx.Client(timeout=15.0) as c:
                r = c.get(f"{self._url}/findings/active", params={"status": status})
                r.raise_for_status()
                data = r.json()
                return data.get("findings", [])
        except Exception:
            return []

    def trigger_monitor(self) -> dict:
        """
        Trigger all 15 monitor checks immediately.

        Returns a per-source summary: ``{sources: {governance: {...}, gateway: {...}, quality: {...}}}``.
        """
        try:
            with httpx.Client(timeout=120.0) as c:
                r = c.post(f"{self._url}/monitor/run")
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}

    # ── System state ───────────────────────────────────────────────────────────

    def get_system_state(self) -> dict:
        """
        Return a live snapshot: HITL queue, policy violations, incidents, circuit breakers.
        """
        try:
            with httpx.Client(timeout=15.0) as c:
                r = c.get(f"{self._url}/system-state")
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}

    def get_system_health(self) -> dict:
        """
        Return aggregate health across all modules (M1, M2, M3).
        Delegates to governance-service internally.
        """
        return self.get_system_state()

    # ── Health ─────────────────────────────────────────────────────────────────

    def health(self) -> bool:
        """Return ``True`` if the EvalGov coordinator is reachable."""
        try:
            with httpx.Client(timeout=5.0) as c:
                return c.get(f"{self._url}/health").status_code == 200
        except Exception:
            return False

    def __repr__(self) -> str:
        return f"IntelligenceClient(evalgov_url={self._url!r})"
