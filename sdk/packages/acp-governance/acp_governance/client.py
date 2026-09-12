"""
acp_governance.client
~~~~~~~~~~~~~~~~~~~~~
Client for the AI Control Plane Governance & Enforcement API (M2).

Before executing sensitive operations, call ``check_policy()`` and inspect
the decision. For HITL-required actions, call ``request_approval()`` and
await operator response via ``get_approval_status()``.
"""

from __future__ import annotations

from typing import Any

import httpx


class GovernanceClient:
    """
    Client for the ACP Governance Service (M2).

    Args:
        governance_url: Base URL of the governance-service
                        (e.g. ``http://localhost:8002``).
        agent_name:     Calling agent name — used for trust scores, incidents,
                        policy decisions, and circuit breaker lookups.
        system_id:      Calling system identifier — scopes policy decisions.

    Example::

        from acp_governance import GovernanceClient

        gov = GovernanceClient(
            governance_url="http://localhost:8002",
            agent_name="summarizer",
            system_id="my-product",
        )

        # Gate a sensitive action behind a policy check
        decision = gov.check_policy(
            action="send_email",
            context={"recipient": "user@example.com", "data_classification": "pii"},
        )
        if decision.get("decision") == "block":
            raise PermissionError(f"Blocked: {decision.get('reason')}")

        # Request human approval for an irreversible action
        req = gov.request_approval(
            action="delete_all_records",
            context={"table": "users", "count": 50000},
            reason="End-of-retention cleanup",
        )
        approval_id = req["id"]
    """

    def __init__(
        self,
        governance_url: str = "http://localhost:8002",
        agent_name: str = "agent",
        system_id: str = "*",
    ) -> None:
        self._url = governance_url.rstrip("/")
        self.agent_name = agent_name
        self.system_id = system_id

    # ── Policy ─────────────────────────────────────────────────────────────────

    def check_policy(self, action: str, context: dict[str, Any] | None = None) -> dict:
        """
        Run a policy check for the given action.

        Returns a dict with at least ``decision`` (``"allow"`` | ``"block"`` | ``"review"``)
        and ``reason``. On HTTP or network failure returns ``{"decision": "allow", "error": ...}``.
        """
        payload = {
            "agent_name": self.agent_name,
            "system_id": self.system_id,
            "action": action,
            "context": context or {},
        }
        return self._post("/policy/check", payload, default={"decision": "allow"})

    # ── Circuit breaker ────────────────────────────────────────────────────────

    def get_circuit_breaker(self) -> dict:
        """Return circuit breaker state for this agent."""
        return self._get(f"/circuit-breakers/{self.agent_name}")

    def is_open(self) -> bool:
        """Return ``True`` if the circuit breaker for this agent is OPEN."""
        cb = self.get_circuit_breaker()
        return cb.get("state", "closed").upper() == "OPEN"

    # ── Trust ──────────────────────────────────────────────────────────────────

    def get_trust_score(self) -> float:
        """Return the current trust score for this agent (0.0–1.0)."""
        data = self._get(f"/trust-scores/{self.agent_name}")
        return float(data.get("score", 1.0))

    # ── HITL ───────────────────────────────────────────────────────────────────

    def request_approval(
        self,
        action: str,
        context: dict[str, Any] | None = None,
        reason: str = "",
    ) -> dict:
        """
        Submit a human-in-the-loop approval request.

        Returns the created HITL entry including its ``id``. Poll with
        :meth:`get_approval_status` until the decision is ``approved`` or ``rejected``.
        """
        payload = {
            "agent_name": self.agent_name,
            "system_id": self.system_id,
            "action": action,
            "context": context or {},
            "reason": reason,
        }
        return self._post("/hitl/request", payload)

    def get_approval_status(self, approval_id: str) -> dict:
        """Poll a HITL request by ID. Returns ``status``: pending | approved | rejected."""
        return self._get(f"/hitl/{approval_id}")

    def wait_for_approval(
        self,
        approval_id: str,
        poll_interval: float = 5.0,
        timeout: float = 300.0,
    ) -> dict:
        """
        Block until the HITL request is decided or ``timeout`` seconds pass.

        Returns the final approval dict. Raises ``TimeoutError`` on timeout.
        """
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            status = self.get_approval_status(approval_id)
            if status.get("status") in ("approved", "rejected"):
                return status
            time.sleep(poll_interval)
        raise TimeoutError(f"HITL request {approval_id} not decided within {timeout}s")

    # ── Incidents ──────────────────────────────────────────────────────────────

    def create_incident(
        self,
        title: str,
        severity: str = "P2",
        description: str = "",
    ) -> dict:
        """Create a governance incident for this agent."""
        payload = {
            "agent_name": self.agent_name,
            "system_id": self.system_id,
            "title": title,
            "severity": severity,
            "description": description,
        }
        return self._post("/incidents", payload)

    # ── Safety ─────────────────────────────────────────────────────────────────

    def report_safety_event(
        self,
        event_type: str,
        details: str,
        detected: bool = True,
    ) -> dict:
        """Report a safety or identity event."""
        payload = {
            "agent_name": self.agent_name,
            "system_id": self.system_id,
            "event_type": event_type,
            "details": details,
            "detected": detected,
        }
        return self._post("/safety/events", payload)

    # ── Health ─────────────────────────────────────────────────────────────────

    def health(self) -> bool:
        """Return ``True`` if the governance service is reachable."""
        try:
            with httpx.Client(timeout=5.0) as c:
                return c.get(f"{self._url}/health").status_code == 200
        except Exception:
            return False

    # ── Internal ───────────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict, default: dict | None = None) -> dict:
        try:
            with httpx.Client(timeout=30.0) as c:
                r = c.post(f"{self._url}{path}", json=payload)
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            result = default.copy() if default else {}
            result["error"] = str(exc)
            return result

    def _get(self, path: str) -> dict:
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(f"{self._url}{path}")
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}

    def __repr__(self) -> str:
        return (
            f"GovernanceClient(governance_url={self._url!r}, "
            f"agent_name={self.agent_name!r}, system_id={self.system_id!r})"
        )
