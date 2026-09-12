"""Tool implementations and schemas for the EvalGov Coordinator (M4).

This coordinator delegates to three specialized sub-agents:
  - M1 Eval Agent     (eval runs, benchmarks, scores, traces, costs, compliance)
  - M2 Governance Agent (HITL, circuit breakers, incidents, trust, findings)
  - M3 Gateway Agent  (routing, keys, A/B tests, shadow rules, proposals)

Plus three direct tools: get_system_health, get_proactive_observations, record_change_followup.
"""

import json
import os
from typing import Any

import httpx
import structlog

from db import AgentDB

log = structlog.get_logger()

GOV_URL        = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
GATEWAY_URL    = os.getenv("GATEWAY_URL",            "http://agent-gateway:8080")
_GATEWAY_ADMIN = os.getenv("GATEWAY_MASTER_KEY",     "")

EVAL_AGENT_URL       = os.getenv("EVAL_AGENT_URL",       "http://localhost:8001")
GOVERNANCE_AGENT_URL = os.getenv("GOVERNANCE_AGENT_URL", "http://localhost:8004")
GATEWAY_AGENT_URL    = os.getenv("GATEWAY_AGENT_URL",    "http://localhost:8005")

EVALGOV_URL = os.getenv("EVALGOV_AGENT_URL", "http://localhost:8003")

_db: AgentDB | None = None


def get_db() -> AgentDB:
    global _db
    if _db is None:
        _db = AgentDB()
    return _db


def _gw_headers() -> dict:
    h: dict = {}
    if _GATEWAY_ADMIN:
        h["x-gateway-admin-key"] = _GATEWAY_ADMIN
    return h


def _gov(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{GOV_URL}{path}", params=params or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("gov_tool_error", path=path, error=str(exc))
        return {"error": str(exc)}


def _gov_post(path: str, body: dict | None = None) -> Any:
    try:
        r = httpx.post(f"{GOV_URL}{path}", json=body or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _gw(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{GATEWAY_URL}{path}", params=params or {},
                      headers=_gw_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("gateway_tool_error", path=path, error=str(exc))
        return {"error": str(exc)}


# ── Delegate tools ─────────────────────────────────────────────────────────────

def call_eval_agent(inputs: dict) -> str:
    """Delegate a query to the M1 Eval Agent."""
    query = inputs.get("query", "")
    history = inputs.get("history", [])
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        r = httpx.post(
            f"{EVAL_AGENT_URL}/chat",
            json={"message": query, "history": history},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        return json.dumps({"response": data.get("response", ""), "tool_calls": data.get("tool_calls", [])}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def call_governance_agent(inputs: dict) -> str:
    """Delegate a query to the M2 Governance Agent."""
    query = inputs.get("query", "")
    history = inputs.get("history", [])
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        r = httpx.post(
            f"{GOVERNANCE_AGENT_URL}/chat",
            json={"message": query, "history": history},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        return json.dumps({"response": data.get("response", ""), "tool_calls": data.get("tool_calls", [])}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def call_gateway_agent(inputs: dict) -> str:
    """Delegate a query to the M3 Gateway Agent."""
    query = inputs.get("query", "")
    history = inputs.get("history", [])
    if not query:
        return json.dumps({"error": "query is required"})
    try:
        r = httpx.post(
            f"{GATEWAY_AGENT_URL}/chat",
            json={"message": query, "history": history},
            timeout=120,
        )
        r.raise_for_status()
        data = r.json()
        return json.dumps({"response": data.get("response", ""), "tool_calls": data.get("tool_calls", [])}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Direct tools (coordinator handles these itself) ────────────────────────────

def get_system_health(_: dict) -> str:
    """Aggregate snapshot of the entire system health."""
    gov = _gov("/governance/summary") or {}
    cbs = _gov("/enforcement/circuit-breakers") or []
    hitl = _gov("/hitl/queue?status=pending&limit=50") or []
    incidents = _gov("/incidents?status=open&limit=10") or []
    trust = _gov("/enforcement/trust-scores") or []
    rogue = _gov("/enforcement/rogue-assessments") or []
    open_cbs = [c for c in cbs if isinstance(c, dict) and c.get("state") == "OPEN"]
    quarantine_rec = [r for r in rogue if isinstance(r, dict) and r.get("quarantine_recommended")]
    return json.dumps({
        "pending_hitl": len(hitl),
        "open_incidents": len(incidents) if isinstance(incidents, list) else incidents.get("total", 0),
        "circuit_breakers_open": len(open_cbs),
        "agents_with_quarantine_flag": len(quarantine_rec),
        "trust_scores": [{"agent": t.get("agent_role"), "score": t.get("trust_score"), "tier": t.get("trust_tier")} for t in trust[:10]],
        "open_cbs": [{"agent": c.get("agent_role"), "state": c.get("state"), "reason": c.get("quarantine_reason", "")} for c in open_cbs],
        "governance_summary": gov,
    }, default=str)


def get_proactive_observations(inputs: dict) -> str:
    """Fetch recent proactive observations the watcher has generated."""
    limit = int(inputs.get("limit", 10))
    try:
        r = httpx.get(f"{EVALGOV_URL}/observations?limit={limit}", timeout=10)
        r.raise_for_status()
        return json.dumps(r.json(), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def record_change_followup(inputs: dict) -> str:
    """Record a pending followup so the watcher checks the outcome in 2 hours."""
    title = inputs.get("title", "").strip()
    detail = inputs.get("detail", "").strip()
    agent_role = inputs.get("agent_role", "")
    if not title:
        return json.dumps({"error": "title is required"})
    try:
        from watcher import _save_observation as _so
        _so("info", "followup", title, detail, agent_role)
        return json.dumps({"status": "followup_recorded", "title": title})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Tool dispatcher ────────────────────────────────────────────────────────────

TOOL_MAP = {
    "call_eval_agent":            call_eval_agent,
    "call_governance_agent":      call_governance_agent,
    "call_gateway_agent":         call_gateway_agent,
    "get_system_health":          get_system_health,
    "get_proactive_observations": get_proactive_observations,
    "record_change_followup":     record_change_followup,
}


def execute_tool(name: str, inputs: dict) -> str:
    fn = TOOL_MAP.get(name)
    if not fn:
        return json.dumps({"error": f"Unknown tool: {name}"})
    try:
        return fn(inputs)
    except Exception as exc:
        log.error("tool_execution_error", tool=name, error=str(exc))
        return json.dumps({"error": str(exc)})


# ── Tool schemas ───────────────────────────────────────────────────────────────

TOOL_SCHEMAS = [
    {
        "name": "call_eval_agent",
        "description": (
            "Delegate a query to the M1 Eval Agent — the specialist for eval runs, "
            "benchmarks, eval scores, OTel traces, agent performance, token costs, "
            "error rates, safety events, thresholds, agent budgets, version pins, "
            "lifecycle changes, model registry, compliance scorecard, risk register, "
            "prompt detail, and search_prompts. "
            "Route any question about eval testing, benchmark results, trace data, "
            "costs, or compliance data to this agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string", "description": "REQUIRED. The full question or instruction for the eval agent."},
                "history": {"type": "array",  "items": {"type": "object"}, "description": "Optional prior conversation turns to provide context."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "call_governance_agent",
        "description": (
            "Delegate a query to the M2 Governance Agent — the specialist for HITL "
            "approvals/rejections, circuit breaker states and resets, agent quarantine, "
            "trust scores, rogue assessments, incidents (open/resolve), anomaly detection, "
            "burn rates, quality gate decisions, gate audit log, policy violations, "
            "policy decisions, proactive monitor findings (acknowledge/resolve/bulk ops), "
            "compliance reports, and reliability summaries. "
            "Route any question about HITL, CB, incidents, trust, policy, or findings to this agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string", "description": "REQUIRED. The full question or instruction for the governance agent."},
                "history": {"type": "array",  "items": {"type": "object"}, "description": "Optional prior conversation turns to provide context."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "call_gateway_agent",
        "description": (
            "Delegate a query to the M3 Gateway Agent — the specialist for gateway "
            "call stats, routing decisions, change proposals, A/B tests (list/create/stop/delete/results), "
            "shadow vs primary comparison, shadow rules (list/create/delete), "
            "prompt mods (list/create/delete), routing policies (list/create/delete), "
            "API keys (list/create/update/revoke), key rejection events, "
            "and traffic management: endpoint pools (list/create/delete), pool endpoints (add/remove), "
            "traffic policies binding agent roles to pools (list/create/delete), and live pool traffic stats. "
            "Route any question about routing, A/B experiments, shadow mode, keys, gateway config, "
            "or traffic management (pools, load balancing, endpoint groups) to this agent."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query":   {"type": "string", "description": "REQUIRED. The full question or instruction for the gateway agent."},
                "history": {"type": "array",  "items": {"type": "object"}, "description": "Optional prior conversation turns to provide context."},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_system_health",
        "description": (
            "Get a complete real-time health snapshot of the entire AI governance system: "
            "pending HITL requests, open circuit breakers, open incidents, quarantine flags, "
            "and trust scores for all agents. "
            "Call this first when the user asks 'what's wrong', 'full status', or a broad health question."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_proactive_observations",
        "description": (
            "Fetch proactive observations generated by the background watcher: "
            "quality regressions, cost spikes, open circuit breakers, shadow mode opportunities, "
            "and followup outcomes. Call this at the start of every conversation."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "description": "Max observations to return. Default 10."}},
        },
    },
    {
        "name": "record_change_followup",
        "description": (
            "Record a pending followup observation so the watcher checks the outcome in 2 hours. "
            "Call this after every configuration change that was executed via a sub-agent "
            "(routing policy, A/B test, prompt mod, etc.)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "title":      {"type": "string", "description": "REQUIRED. Short description of what was changed."},
                "detail":     {"type": "string", "description": "Full context for the followup check."},
                "agent_role": {"type": "string", "description": "Agent role affected by the change."},
            },
            "required": ["title"],
        },
    },
]
