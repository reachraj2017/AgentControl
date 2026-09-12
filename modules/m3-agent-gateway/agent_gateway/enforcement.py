"""Enforcement — governance service client for the gateway.

Wraps three governance-service calls:
  phase2_enabled()   — cached flag: is enforcement active?
  gate_check()       — pre-call gate: CB / HITL / policy check
  wait_for_hitl()    — poll until operator approves or timeout

All calls fail-open: if governance service is unreachable,
the call is allowed through and logged as 'gov_unreachable'.
"""

import logging
import os
import time

import httpx

log = logging.getLogger("gateway.enforcement")

_GOV_URL      = os.getenv("GOVERNANCE_SERVICE_URL", "http://governance-service:8002")
_CB_TTL       = 30.0
_HITL_POLL    = 3.0
_HITL_TIMEOUT = float(os.getenv("HITL_TIMEOUT_SECONDS", "120"))

# Simple in-process cache: [enabled_bool, expires_monotonic]
_p2_cache: list = [False, 0.0]


def phase2_enabled() -> bool:
    """Return whether Phase 2 (real-time enforcement) is active. Cached 30 s."""
    if _p2_cache[1] and time.monotonic() < _p2_cache[1]:
        return bool(_p2_cache[0])
    try:
        with httpx.Client(timeout=2.0) as client:
            resp = client.get(f"{_GOV_URL}/enforcement/phase2/status")
            resp.raise_for_status()
            val = bool(resp.json().get("phase2_enabled", False))
            _p2_cache[0] = val
            _p2_cache[1] = time.monotonic() + _CB_TTL
            return val
    except Exception as e:
        log.debug("phase2_enabled check failed (fail open): %s", e)
    _p2_cache[1] = time.monotonic() + 5.0  # short retry backoff on error
    return False


def gate_check(
    agent_role: str,
    system_id:  str,
    query:      str,
    trace_id:   str,
    run_id:     str,
) -> tuple[str, str]:
    """Call governance gate before forwarding.

    Returns (decision, request_id).
    decision: auto_approve | flag | pause | block
    """
    try:
        with httpx.Client(timeout=3.0) as client:
            resp = client.post(
                f"{_GOV_URL}/gate/check",
                json={
                    "action_type": "agent_invoke",
                    "agent_role":  agent_role,
                    "context": {
                        "agent":  agent_role,
                        "system": system_id,
                        "query":  query[:500],
                    },
                    "trace_id": trace_id,
                    "run_id":   run_id,
                },
            )
            resp.raise_for_status()
            data = resp.json()
            return data.get("decision", "auto_approve"), data.get("request_id", "")
    except Exception as e:
        log.debug("gate_check failed (fail open): %s", e)
    return "auto_approve", ""


def wait_for_hitl(request_id: str) -> str:
    """Block until operator approves/rejects or timeout.

    Returns 'approved' | 'rejected' | 'expired' | 'timeout'
    """
    deadline = time.monotonic() + _HITL_TIMEOUT
    while time.monotonic() < deadline:
        try:
            with httpx.Client(timeout=2.0) as client:
                resp = client.get(f"{_GOV_URL}/hitl/{request_id}/status")
                if resp.status_code == 200:
                    status = resp.json().get("status", "pending")
                    if status == "approved":
                        return "approved"
                    if status in ("rejected", "expired"):
                        return status
        except Exception:
            pass
        time.sleep(_HITL_POLL)
    return "timeout"
