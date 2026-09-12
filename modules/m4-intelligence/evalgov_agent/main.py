"""EvalGov Intelligence Agent — FastAPI service.

Endpoints:
  GET  /health
  POST /chat              Send a message to the agent
  POST /chat/reset        Reset (clear) conversation (stateless — client manages history)
  GET  /findings          List proactive monitor findings (paginated)
  GET  /findings/active   Active findings grouped by severity
  POST /findings/{id}/acknowledge   Acknowledge a finding
  POST /findings/{id}/resolve       Resolve a finding
  POST /findings/acknowledge-bulk   Bulk acknowledge by ids
  POST /monitor/run       Manually trigger all monitor checks now
  GET  /observations      Active findings in legacy observation format (for coordinator tool)
  GET  /system-state      Live system state snapshot
  GET  /mcp/sse           MCP SSE stream for Claude Code / external agents
  POST /mcp/messages/     MCP message handler
"""

import asyncio
import os
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel

log = structlog.get_logger()


# ── Lifespan ──────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    from db import AgentDB
    from monitor import ProactiveMonitor

    db = AgentDB()
    db.ensure_tables()
    app.state.db = db

    monitor = ProactiveMonitor(db)
    monitor.start()
    app.state.monitor = monitor

    log.info("evalgov_agent_started")
    yield

    monitor.stop()
    log.info("evalgov_agent_stopped")


app = FastAPI(title="EvalGov Intelligence Agent", lifespan=lifespan)

# ── Mount MCP server (optional — graceful degradation if mcp not installed) ───

try:
    from mcp_server import build_mcp_app
    app.mount("/mcp", build_mcp_app())
    log.info("mcp_server_mounted", path="/mcp")
except Exception as exc:
    log.warning("mcp_server_unavailable", reason=str(exc))


# ── Pydantic models ───────────────────────────────────────────────────────────

class ChatRequest(BaseModel):
    message: str
    history: list[dict] = []  # [{role, content}] — client sends full history


class ChatResponse(BaseModel):
    response: str
    tool_calls: list[dict] = []


class AckRequest(BaseModel):
    acknowledged_by: str = "operator"


class BulkAckRequest(BaseModel):
    ids: list[str]
    acknowledged_by: str = "operator"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok", "service": "evalgov-agent"}


@app.post("/chat", response_model=ChatResponse)
def chat(req: ChatRequest):
    from agent import chat as agent_chat
    try:
        response_text, tool_calls = agent_chat(req.message, req.history)
        return ChatResponse(response=response_text, tool_calls=tool_calls)
    except Exception as exc:
        log.error("chat_error", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/findings")
def get_findings(
    hours: int = Query(default=24),
    severity: str = Query(default=""),
    status: str = Query(default="active"),
    limit: int = Query(default=30),
):
    from db import AgentDB
    db: AgentDB = app.state.db
    rows = db.get_findings(hours=hours, severity=severity, status=status, limit=limit)
    return {"findings": rows, "count": len(rows)}


@app.get("/findings/active")
def get_active_findings(hours: int = Query(default=24)):
    db = app.state.db
    rows = db.get_all_active_findings(hours=hours)
    by_severity = {"critical": [], "high": [], "medium": [], "low": [], "info": []}
    for f in rows:
        sev = f.get("severity", "medium")
        by_severity.setdefault(sev, []).append(f)
    return {"findings": rows, "by_severity": by_severity, "count": len(rows)}


@app.post("/findings/{finding_id}/acknowledge")
def acknowledge_finding(finding_id: str, req: AckRequest):
    db = app.state.db
    db.acknowledge_finding(finding_id, req.acknowledged_by)
    return {"status": "acknowledged", "finding_id": finding_id}


@app.post("/findings/{finding_id}/resolve")
def resolve_finding(finding_id: str):
    db = app.state.db
    db.resolve_finding(finding_id)
    return {"status": "resolved", "finding_id": finding_id}


@app.post("/findings/acknowledge-bulk")
def acknowledge_findings_bulk(req: BulkAckRequest):
    db = app.state.db
    for fid in req.ids:
        db.acknowledge_finding(fid, req.acknowledged_by)
    return {"acknowledged": len(req.ids)}


@app.post("/findings/bulk-resolve-quality-gates")
def bulk_resolve_quality_gate_findings():
    """Acknowledge all active quality gate hold and block findings."""
    db = app.state.db
    count = db.bulk_acknowledge_quality_gate_findings()
    return {"acknowledged": count}


@app.post("/monitor/run")
async def trigger_monitor() -> dict:
    """Manually trigger all monitor checks immediately. Returns per-source summary."""
    monitor = app.state.monitor
    results = await asyncio.to_thread(monitor.run_once)
    return {"status": "completed", "sources": results}


@app.get("/observations")
def get_observations(limit: int = Query(default=10)) -> dict:
    """Return active findings in legacy observation format for the coordinator tool."""
    db = app.state.db
    rows = db.get_all_active_findings(hours=24)
    # Map findings to the observation shape the coordinator tool expects
    observations = [
        {
            "observation_id": r.get("finding_id"),
            "severity":       r.get("severity"),
            "category":       r.get("finding_type"),
            "title":          r.get("title"),
            "detail":         r.get("summary"),
            "agent_role":     r.get("affected_agent"),
            "created_at":     str(r.get("created_at", "")),
        }
        for r in rows[:limit]
    ]
    return {"observations": observations, "count": len(observations)}


@app.post("/observations/acknowledge")
async def ack_observations(body: dict) -> dict:
    """Mark findings as acknowledged (backward compat for the coordinator tool)."""
    ids = body.get("observation_ids", [])
    db = app.state.db
    for fid in ids:
        db.acknowledge_finding(fid, "operator")
    return {"acknowledged": len(ids)}


@app.get("/system-state")
def get_system_state(hours: int = Query(default=1)):
    """Live system state: HITL queue, policy violations, incidents, circuit breakers."""
    db = app.state.db
    return {
        "hitl_queue":          db.get_hitl_queue_live(hours=24, limit=25),
        "policy_violations":   db.get_policy_decisions(hours=24, decision="block", limit=25),
        "incidents":           db.get_incidents_live(hours=24, limit=25),
        "circuit_breakers":    db.get_circuit_breakers_live(),
        "key_events":          db.get_gateway_key_events_live(hours=1, limit=25),
        "pending_proposals":   db.get_pending_change_proposals(limit=25),
    }
