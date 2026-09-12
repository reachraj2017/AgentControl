"""Tool implementations and schemas for the M2 Governance Agent."""

import json
import os
from typing import Any

import httpx
import structlog
from clickhouse_driver import Client

log = structlog.get_logger()

GOV_URL = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")


# ── DB helpers ─────────────────────────────────────────────────────────────────

class AgentDB:
    def __init__(self):
        self.client = Client(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "9000")),
            database=os.getenv("CLICKHOUSE_DB", "otel"),
            user=os.getenv("CLICKHOUSE_USER", "default"),
            password=os.getenv("CLICKHOUSE_PASSWORD", ""),
            settings={"use_numpy": False},
        )

    def _run(self, sql: str, params: dict | None = None) -> list[dict]:
        rows, cols = self.client.execute(sql, params or {}, with_column_types=True)
        names = [c[0] for c in cols]
        return [dict(zip(names, r)) for r in rows]

    def _exec(self, sql: str, params: dict | None = None):
        self.client.execute(sql, params or {})

    def get_findings(
        self, hours: int = 24, severity: str = "", status: str = "active", limit: int = 50
    ) -> list[dict]:
        conds = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {"limit": limit}
        if severity:
            conds.append("severity = %(sev)s")
            params["sev"] = severity
        if status:
            conds.append("status = %(status)s")
            params["status"] = status
        where = " AND ".join(conds)
        return self._run(
            f"SELECT finding_id, finding_type, severity, title, summary, rca, "
            f"recommendation, affected_agent, status, acknowledged_by, created_at "
            f"FROM otel.gov_agent_findings FINAL WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )

    def acknowledge_finding(self, finding_id: str, acknowledged_by: str):
        rows = self._run(
            "SELECT * FROM otel.gov_agent_findings FINAL "
            "WHERE finding_id = %(fid)s LIMIT 1",
            {"fid": finding_id},
        )
        if not rows:
            return
        r = rows[0]
        s = lambda v: str(v or "").replace("'", "\\'")
        self._exec(
            f"INSERT INTO otel.gov_agent_findings "
            f"(finding_id, finding_type, severity, title, summary, rca, recommendation, "
            f" signal_data, affected_agent, status, acknowledged_by, created_at, updated_at) VALUES "
            f"('{s(r['finding_id'])}', '{s(r['finding_type'])}', '{s(r['severity'])}', "
            f"'{s(r['title'])}', '{s(r['summary'])}', '{s(r['rca'])}', '{s(r['recommendation'])}', "
            f"'{{}}', '{s(r['affected_agent'])}', 'acknowledged', "
            f"'{s(acknowledged_by)}', '{str(r['created_at'])[:19]}', now())"
        )

    def resolve_finding(self, finding_id: str):
        rows = self._run(
            "SELECT * FROM otel.gov_agent_findings FINAL WHERE finding_id = %(fid)s LIMIT 1",
            {"fid": finding_id},
        )
        if not rows:
            return
        r = rows[0]
        s = lambda v: str(v or "").replace("'", "\\'")
        self._exec(
            f"INSERT INTO otel.gov_agent_findings "
            f"(finding_id, finding_type, severity, title, summary, rca, recommendation, "
            f" signal_data, affected_agent, status, acknowledged_by, created_at, updated_at) VALUES "
            f"('{s(r['finding_id'])}', '{s(r['finding_type'])}', '{s(r['severity'])}', "
            f"'{s(r['title'])}', '{s(r['summary'])}', '{s(r['rca'])}', '{s(r['recommendation'])}', "
            f"'{{}}', '{s(r['affected_agent'])}', 'resolved', "
            f"'{s(r['acknowledged_by'])}', '{str(r['created_at'])[:19]}', now())"
        )

    def get_policy_decisions(
        self, hours: int = 24, agent_role: str = "", decision: str = "", limit: int = 50
    ) -> list[dict]:
        h = int(hours)
        conds = [f"ts >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 200)}
        if decision:
            conds.append("decision = %(dec)s")
            params["dec"] = decision
        where = " AND ".join(conds)
        return self._run(
            f"SELECT decision_id, trace_id, run_id, metric, decision, "
            f"value, threshold, message, ts "
            f"FROM otel.gov_policy_decisions "
            f"WHERE {where} "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            params,
        )


_db: AgentDB | None = None


def get_db() -> AgentDB:
    global _db
    if _db is None:
        _db = AgentDB()
    return _db


def _gov(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{GOV_URL}{path}", params=params or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("gov_tool_error", path=path, error=str(exc))
        return {"error": str(exc)}


def _gov_put(path: str, body: dict) -> Any:
    try:
        r = httpx.put(f"{GOV_URL}{path}", json=body, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _gov_post(path: str, body: dict | None = None) -> Any:
    try:
        r = httpx.post(f"{GOV_URL}{path}", json=body or {}, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── Tool implementations ───────────────────────────────────────────────────────

def get_hitl_queue(inputs: dict) -> str:
    status = inputs.get("status", "")
    hours = inputs.get("hours", 24)
    path = f"/hitl/queue?limit=100&hours={hours}"
    if status:
        path += f"&status={status}"
    data = _gov(path) or []
    return json.dumps(data, default=str)


def approve_hitl(inputs: dict) -> str:
    request_id = inputs.get("request_id", "")
    reviewer = inputs.get("reviewer", "governance-agent")
    notes = inputs.get("notes", "Approved via Governance Agent")
    result = _gov_put(f"/hitl/{request_id}", {"status": "approved", "reviewer": reviewer, "notes": notes})
    return json.dumps(result, default=str)


def reject_hitl(inputs: dict) -> str:
    request_id = inputs.get("request_id", "")
    reviewer = inputs.get("reviewer", "governance-agent")
    notes = inputs.get("notes", "Rejected via Governance Agent")
    result = _gov_put(f"/hitl/{request_id}", {"status": "rejected", "reviewer": reviewer, "notes": notes})
    return json.dumps(result, default=str)


def bulk_approve_hitl(inputs: dict) -> str:
    reviewer = inputs.get("reviewer", "operator")
    notes = inputs.get("notes", "Bulk approved via Governance Agent")
    data = _gov("/hitl/queue?status=pending&limit=200") or []
    requests_list = data if isinstance(data, list) else data.get("requests", []) if isinstance(data, dict) else []
    approved, errors = [], []
    for req in requests_list:
        rid = req.get("request_id", "")
        if not rid:
            continue
        result = _gov_put(f"/hitl/{rid}", {"status": "approved", "reviewer": reviewer, "notes": notes})
        if isinstance(result, dict) and result.get("error"):
            errors.append({"request_id": rid, "error": result["error"]})
        else:
            approved.append(rid)
    return json.dumps({"approved_count": len(approved), "errors": errors})


def get_circuit_breakers(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    data = _gov("/enforcement/circuit-breakers") or []
    if agent_role and isinstance(data, list):
        data = [c for c in data if c.get("agent_role") == agent_role]
    return json.dumps(data, default=str)


def reset_circuit_breaker(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    result = _gov_post(f"/enforcement/circuit-breakers/{agent_role}/reset")
    return json.dumps(result, default=str)


def quarantine_agent(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    reason = inputs.get("reason", "Manual quarantine via Governance Agent")
    result = _gov_post(f"/enforcement/circuit-breakers/{agent_role}/quarantine", {"reason": reason})
    return json.dumps(result, default=str)


def get_trust_scores(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    if agent_role:
        history = _gov(f"/enforcement/trust-scores/{agent_role}/history?hours={inputs.get('hours', 24)}")
        return json.dumps(history, default=str)
    data = _gov("/enforcement/trust-scores") or []
    return json.dumps(data, default=str)


def get_rogue_assessments(_: dict) -> str:
    data = _gov("/enforcement/rogue-assessments") or []
    return json.dumps(data, default=str)


def get_incidents(inputs: dict) -> str:
    status = inputs.get("status", "")
    hours = inputs.get("hours", 48)
    params: dict = {"limit": 50}
    if status:
        params["status"] = status
    if hours:
        params["hours"] = hours
    data = _gov("/incidents", params) or []
    return json.dumps(data, default=str)


def resolve_incident(inputs: dict) -> str:
    incident_id = inputs.get("incident_id", "")
    result = _gov_put(f"/incidents/{incident_id}", {"status": "resolved"})
    return json.dumps(result, default=str)


def bulk_resolve_incidents(inputs: dict) -> str:
    data = _gov("/incidents", {"status": "open", "limit": 200}) or []
    incidents = data if isinstance(data, list) else []
    resolved, errors = [], []
    for inc in incidents:
        iid = inc.get("incident_id", "")
        if not iid:
            continue
        result = _gov_put(f"/incidents/{iid}", {"status": "resolved"})
        if isinstance(result, dict) and result.get("error"):
            errors.append({"incident_id": iid, "error": result["error"]})
        else:
            resolved.append(iid)
    return json.dumps({"resolved_count": len(resolved), "errors": errors})


def get_anomalies(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    data = _gov("/anomalies", {"hours": hours}) or []
    return json.dumps(data, default=str)


def get_burn_rates(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    if agent_role:
        data = _gov(f"/enforcement/burn-rates/{agent_role}")
    else:
        data = _gov("/enforcement/burn-rates")
    return json.dumps(data or {}, default=str)


def get_quality_gate_decisions(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    agent_role = inputs.get("agent_role", "")
    params: dict = {"hours": hours, "limit": 100}
    if agent_role:
        params["agent_role"] = agent_role
    data = _gov("/quality-gates/decisions", params) or {}
    return json.dumps(data, default=str)


def get_gate_audit_log(inputs: dict) -> str:
    hours = inputs.get("hours", 6)
    data = _gov("/governance/summary") or {}
    audit_data = {
        "gate_checks": data.get("gate_checks_24h", 0),
        "gate_blocks": data.get("gate_blocks_24h", 0),
        "note": f"Detailed audit via /gate/audit endpoint. Summary covers last {hours}h."
    }
    return json.dumps(audit_data, default=str)


def get_policy_violations(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    data = _gov("/governance/summary") or {}
    return json.dumps({
        "policy_blocks_24h": data.get("policy_blocks_24h", 0),
        "policy_flags_24h": data.get("policy_flags_24h", 0),
        "note": "Policy records are audit-only; use /policies for rule definitions."
    }, default=str)


def get_policy_decisions(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    agent_role = inputs.get("agent_role", "")
    decision = inputs.get("decision", "")
    limit = inputs.get("limit", 50)
    try:
        rows = get_db().get_policy_decisions(hours, agent_role, decision, limit)
        return json.dumps({"decisions": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_findings(inputs: dict) -> str:
    hours = inputs.get("hours", 8760)
    severity = inputs.get("severity", "")
    status = inputs.get("status", "active")
    try:
        rows = get_db().get_findings(hours, severity, status, limit=100)
        return json.dumps(rows, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def acknowledge_finding(inputs: dict) -> str:
    finding_id = inputs.get("finding_id", "")
    acknowledged_by = inputs.get("acknowledged_by", "operator")
    try:
        get_db().acknowledge_finding(finding_id, acknowledged_by)
        return json.dumps({"status": "acknowledged", "finding_id": finding_id})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def resolve_finding(inputs: dict) -> str:
    finding_id = inputs.get("finding_id", "")
    try:
        get_db().resolve_finding(finding_id)
        return json.dumps({"status": "resolved", "finding_id": finding_id})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def bulk_resolve_findings(inputs: dict) -> str:
    acknowledged_by = inputs.get("acknowledged_by", "operator")
    try:
        db = get_db()
        rows = db.get_findings(hours=8760, status="", limit=500)
        active = [r for r in rows if r.get("status") in ("active", "acknowledged")]
        resolved = []
        errors = []
        for r in active:
            fid = r.get("finding_id", "")
            if not fid:
                continue
            try:
                db.resolve_finding(fid)
                resolved.append(fid)
            except Exception as exc:
                errors.append({"finding_id": fid, "error": str(exc)})
        return json.dumps({"resolved_count": len(resolved), "errors": errors})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def bulk_acknowledge_findings(inputs: dict) -> str:
    acknowledged_by = inputs.get("acknowledged_by", "operator")
    try:
        db = get_db()
        rows = db.get_findings(hours=8760, status="active", limit=500)
        acknowledged = []
        errors = []
        for r in rows:
            fid = r.get("finding_id", "")
            if not fid:
                continue
            try:
                db.acknowledge_finding(fid, acknowledged_by)
                acknowledged.append(fid)
            except Exception as exc:
                errors.append({"finding_id": fid, "error": str(exc)})
        return json.dumps({"acknowledged_count": len(acknowledged), "errors": errors})
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_compliance_report(_: dict) -> str:
    data = _gov("/compliance/report") or {}
    return json.dumps(data, default=str)


def get_reliability_summary(_: dict) -> str:
    data = _gov("/reliability/summary") or []
    return json.dumps(data, default=str)


# ── Tool dispatcher ────────────────────────────────────────────────────────────

TOOL_MAP = {
    "get_hitl_queue":              get_hitl_queue,
    "approve_hitl":                approve_hitl,
    "reject_hitl":                 reject_hitl,
    "bulk_approve_hitl":           bulk_approve_hitl,
    "get_circuit_breakers":        get_circuit_breakers,
    "reset_circuit_breaker":       reset_circuit_breaker,
    "quarantine_agent":            quarantine_agent,
    "get_trust_scores":            get_trust_scores,
    "get_rogue_assessments":       get_rogue_assessments,
    "get_incidents":               get_incidents,
    "resolve_incident":            resolve_incident,
    "bulk_resolve_incidents":      bulk_resolve_incidents,
    "get_anomalies":               get_anomalies,
    "get_burn_rates":              get_burn_rates,
    "get_quality_gate_decisions":  get_quality_gate_decisions,
    "get_gate_audit_log":          get_gate_audit_log,
    "get_policy_violations":       get_policy_violations,
    "get_policy_decisions":        get_policy_decisions,
    "get_findings":                get_findings,
    "acknowledge_finding":         acknowledge_finding,
    "resolve_finding":             resolve_finding,
    "bulk_resolve_findings":       bulk_resolve_findings,
    "bulk_acknowledge_findings":   bulk_acknowledge_findings,
    "get_compliance_report":       get_compliance_report,
    "get_reliability_summary":     get_reliability_summary,
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
        "name": "get_hitl_queue",
        "description": "List HITL (Human-in-the-Loop) approval requests. Agents are blocked waiting for approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["pending", "approved", "rejected", ""], "description": "Filter by status. Empty = all."},
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24). Pending requests always shown."},
            },
        },
    },
    {
        "name": "approve_hitl",
        "description": "Approve a pending HITL request. The blocked agent will immediately resume its action. Always call get_hitl_queue first to find the request_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string", "description": "The request_id from the HITL queue"},
                "reviewer": {"type": "string", "description": "Name or identifier of the approver"},
                "notes": {"type": "string", "description": "Optional approval notes"},
            },
            "required": ["request_id"],
        },
    },
    {
        "name": "reject_hitl",
        "description": "Reject a pending HITL request. The agent will receive a governance block and cannot proceed. Always call get_hitl_queue first to find the request_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "reviewer": {"type": "string"},
                "notes": {"type": "string", "description": "Reason for rejection (required by policy)"},
            },
            "required": ["request_id", "notes"],
        },
    },
    {
        "name": "bulk_approve_hitl",
        "description": "Approve ALL pending HITL requests in one operation. Use when the user asks to approve or clear all pending HITL requests.",
        "input_schema": {
            "type": "object",
            "properties": {
                "reviewer": {"type": "string", "description": "Who is approving (default: operator)"},
                "notes": {"type": "string", "description": "Approval notes applied to all requests"},
            },
        },
    },
    {
        "name": "get_circuit_breakers",
        "description": "Get circuit breaker states for all agents (CLOSED=healthy, OPEN=blocked, HALF_OPEN=recovering). OPEN means all actions are blocked.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Filter to a specific agent. Empty = all agents."},
            },
        },
    },
    {
        "name": "reset_circuit_breaker",
        "description": "Manually reset (close) an agent's circuit breaker, allowing it to resume operations.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_role": {"type": "string"}},
            "required": ["agent_role"],
        },
    },
    {
        "name": "quarantine_agent",
        "description": "Manually quarantine an agent (set CB to OPEN with quarantine flag). All actions blocked until manual reset.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string"},
                "reason": {"type": "string", "description": "Reason for quarantine"},
            },
            "required": ["agent_role", "reason"],
        },
    },
    {
        "name": "get_trust_scores",
        "description": "Get trust scores for all agents (composite score from identity, behavior, compliance, network signals). Lower score = less trustworthy.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Specific agent for history. Empty = latest all agents."},
                "hours": {"type": "integer", "description": "History window in hours (only used when agent_role is specified)"},
            },
        },
    },
    {
        "name": "get_rogue_assessments",
        "description": "Get rogue agent detection assessments: frequency_score, entropy_score, capability_score, composite_score, and quarantine_recommended flag.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_incidents",
        "description": "List governance incidents (open or resolved). Incidents are automatically triggered by safety, identity, reliability, or behavior violations.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "resolved", "contained", ""], "description": "Filter by status"},
                "hours": {"type": "integer", "description": "Look-back window in hours"},
            },
        },
    },
    {
        "name": "resolve_incident",
        "description": "Mark a single incident as resolved by incident_id. Always call get_incidents first to find the incident_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {"incident_id": {"type": "string"}},
            "required": ["incident_id"],
        },
    },
    {
        "name": "bulk_resolve_incidents",
        "description": "Resolve ALL open incidents in one operation. Use when the user asks to resolve or close all incidents.",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "get_anomalies",
        "description": "Get recent anomaly detection events (statistical outliers in agent behavior vs. established baselines).",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Look-back window in hours (default 24)"}},
        },
    },
    {
        "name": "get_burn_rates",
        "description": "Get error budget burn rates for agents. High burn rate = SLO exhaustion approaching.",
        "input_schema": {
            "type": "object",
            "properties": {"agent_role": {"type": "string", "description": "Specific agent or empty for all"}},
        },
    },
    {
        "name": "get_quality_gate_decisions",
        "description": "Get content quality gate decisions (flag/hold/block) triggered by the quality checker for output quality issues.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window"},
                "agent_role": {"type": "string", "description": "Filter by agent"},
            },
        },
    },
    {
        "name": "get_gate_audit_log",
        "description": "Get gate check summary statistics: total gate checks and blocks in the last N hours.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 6)"},
            },
        },
    },
    {
        "name": "get_policy_violations",
        "description": "Get summary of policy engine violations (policy flags and blocks recorded in the audit log).",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer"}},
        },
    },
    {
        "name": "get_policy_decisions",
        "description": "Get policy engine verdicts (warn/block/pass) from gov_policy_decisions. Shows which metric triggered the decision, the observed value vs threshold, and the message.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "agent_role": {"type": "string", "description": "Filter by agent role"},
                "decision": {"type": "string", "enum": ["warn", "block", "pass", ""], "description": "Filter by decision type"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    {
        "name": "get_findings",
        "description": "Get proactive monitor findings (anomalies, RCA analyses, recommendations) generated by the background monitor.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window"},
                "severity": {"type": "string", "enum": ["critical", "high", "medium", "low", ""]},
                "status": {"type": "string", "enum": ["active", "acknowledged", "resolved", ""]},
            },
        },
    },
    {
        "name": "acknowledge_finding",
        "description": "Acknowledge a single proactive monitor finding by finding_id. Always call get_findings first to find the finding_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
                "acknowledged_by": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "resolve_finding",
        "description": "Resolve (close) a single proactive monitor finding by finding_id. Always call get_findings first to find the finding_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "finding_id": {"type": "string"},
            },
            "required": ["finding_id"],
        },
    },
    {
        "name": "bulk_resolve_findings",
        "description": "Resolve ALL active and acknowledged findings in one operation. Use when the user asks to clear, dismiss, or resolve all findings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "acknowledged_by": {"type": "string", "description": "Who is resolving (default: operator)"},
            },
        },
    },
    {
        "name": "bulk_acknowledge_findings",
        "description": "Acknowledge ALL active findings in one operation. Use when the user asks to acknowledge all findings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "acknowledged_by": {"type": "string", "description": "Who is acknowledging (default: operator)"},
            },
        },
    },
    {
        "name": "get_compliance_report",
        "description": "Generate and return a compliance summary report covering all governance categories.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_reliability_summary",
        "description": "Get reliability metrics summary per agent (uptime, error rate, SLO compliance).",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
]
