"""Tool implementations and schemas for the M3 Gateway Agent."""

import json
import os
from typing import Any

import httpx
import structlog
from clickhouse_driver import Client

log = structlog.get_logger()

GATEWAY_URL = os.getenv("GATEWAY_URL", "http://agent-gateway:8080")
_GATEWAY_ADMIN = os.getenv("GATEWAY_MASTER_KEY", "")


def _gw_headers() -> dict:
    h: dict = {}
    if _GATEWAY_ADMIN:
        h["x-gateway-admin-key"] = _GATEWAY_ADMIN
    return h


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

    def get_gateway_call_stats(self, hours: int = 168, agent_role: str = "") -> dict:
        h = int(hours)
        role_cond = f"AND agent_role = '{agent_role.replace(chr(39), '')}'" if agent_role else ""

        call_rows = self._run(f"""
            SELECT agent_role, model_used, model_requested,
                   count()                        AS call_count,
                   round(avg(latency_ms), 0)      AS avg_latency_ms,
                   sum(tokens_in)                 AS total_tokens_in,
                   sum(tokens_out)                AS total_tokens_out,
                   countIf(status = 'error')      AS error_count
            FROM otel.gateway_call_log
            WHERE is_shadow = 0
              AND created_at >= now() - INTERVAL {h} HOUR
              {role_cond}
            GROUP BY agent_role, model_used, model_requested
            ORDER BY agent_role, call_count DESC
        """)

        score_rows = []
        try:
            role_filter = f"AND pe.agent_name = '{agent_role.replace(chr(39), '')}'" if agent_role else ""
            score_rows = self._run(f"""
                SELECT pe.agent_name AS agent_role,
                       gw.model_used,
                       round(avgIf(pe.scores['faithfulness'],          pe.scores['faithfulness']          > 0), 3) AS avg_faithfulness,
                       round(avgIf(pe.scores['relevance'],             pe.scores['relevance']             > 0), 3) AS avg_relevance,
                       round(avgIf(pe.scores['task_success_rate'],     pe.scores['task_success_rate']     > 0), 3) AS avg_task_success,
                       round(avgIf(pe.scores['instruction_following'], pe.scores['instruction_following'] > 0), 3) AS avg_instruction_following,
                       count() AS eval_count
                FROM otel.prompt_evals pe
                LEFT JOIN (
                    SELECT trace_id, model_used
                    FROM otel.gateway_call_log
                    WHERE is_shadow = 0 AND trace_id != ''
                ) gw ON pe.trace_id = gw.trace_id
                WHERE pe.created_at >= now() - INTERVAL {h} HOUR
                  AND gw.model_used != ''
                  {role_filter}
                GROUP BY pe.agent_name, gw.model_used
                ORDER BY pe.agent_name
            """)
        except Exception:
            pass

        return {"call_stats": call_rows, "eval_scores_by_model": score_rows, "hours": h}

    def get_routing_decisions(self, hours: int = 168, agent_role: str = "", limit: int = 50) -> list[dict]:
        h = int(hours)
        conds = [f"ts >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 200)}
        if agent_role:
            conds.append("agent_role = %(role)s")
            params["role"] = agent_role
        where = " AND ".join(conds)
        return self._run(
            f"SELECT decision_id, trace_id, agent_role, complexity_tier, model, "
            f"input_chars, estimated_savings_pct, ts "
            f"FROM otel.gov_routing_decisions "
            f"WHERE {where} "
            f"ORDER BY ts DESC LIMIT %(limit)s",
            params,
        )

    def compare_shadow_vs_primary(self, agent_role: str = "", hours: int = 168) -> dict:
        h = int(hours)
        safe_role = agent_role.replace("'", "")
        role_cond_gw = f"AND agent_role = '{safe_role}'" if safe_role else ""
        role_cond_pe = f"AND pe.agent_name = '{safe_role}'" if safe_role else ""

        shadow_calls = []
        try:
            shadow_calls = self._run(f"""
                SELECT agent_role, model_used,
                       count()                    AS shadow_call_count,
                       round(avg(latency_ms), 0)  AS avg_latency_ms,
                       sum(tokens_in)             AS total_tokens_in,
                       sum(tokens_out)            AS total_tokens_out
                FROM otel.gateway_call_log
                WHERE is_shadow = 1
                  AND created_at >= now() - INTERVAL {h} HOUR
                  {role_cond_gw}
                GROUP BY agent_role, model_used
                ORDER BY agent_role, shadow_call_count DESC
            """)
        except Exception:
            pass

        shadow_scores = []
        try:
            role_filter_se = f"AND agent_role = '{safe_role}'" if safe_role else ""
            shadow_scores = self._run(f"""
                SELECT agent_role, model_used,
                       round(avgIf(scores['faithfulness'],          scores['faithfulness']          > 0), 3) AS avg_faithfulness,
                       round(avgIf(scores['relevance'],             scores['relevance']             > 0), 3) AS avg_relevance,
                       round(avgIf(scores['instruction_following'], scores['instruction_following'] > 0), 3) AS avg_instruction_following,
                       count() AS scored_count
                FROM otel.gateway_shadow_evals
                WHERE scored_at >= now() - INTERVAL {h} HOUR
                  {role_filter_se}
                GROUP BY agent_role, model_used
                ORDER BY agent_role
            """)
        except Exception:
            pass

        primary_scores = []
        try:
            primary_scores = self._run(f"""
                SELECT pe.agent_name AS agent_role,
                       gw.model_used,
                       round(avgIf(pe.scores['faithfulness'],          pe.scores['faithfulness']          > 0), 3) AS avg_faithfulness,
                       round(avgIf(pe.scores['relevance'],             pe.scores['relevance']             > 0), 3) AS avg_relevance,
                       round(avgIf(pe.scores['instruction_following'], pe.scores['instruction_following'] > 0), 3) AS avg_instruction_following,
                       count() AS eval_count
                FROM otel.prompt_evals pe
                LEFT JOIN (
                    SELECT trace_id, model_used
                    FROM otel.gateway_call_log
                    WHERE is_shadow = 0 AND trace_id != ''
                ) gw ON pe.trace_id = gw.trace_id
                WHERE pe.created_at >= now() - INTERVAL {h} HOUR
                  AND gw.model_used != ''
                  {role_cond_pe}
                GROUP BY pe.agent_name, gw.model_used
                ORDER BY pe.agent_name
            """)
        except Exception:
            pass

        return {
            "shadow_call_volume": shadow_calls,
            "shadow_scores": shadow_scores,
            "primary_scores": primary_scores,
            "hours": h,
            "agent_role": agent_role or "all",
        }

    def get_key_rejection_events(self, key_id: str = "", hours: int = 24, limit: int = 100) -> list[dict]:
        h = int(hours)
        conds = [f"ts >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 500)}
        if key_id:
            conds.append("key_id = %(kid)s")
            params["kid"] = key_id
        where = " AND ".join(conds)
        return self._run(
            f"SELECT ts, key_id, key_prefix, agent_role, system_id, "
            f"event_type, http_status, model_requested, detail "
            f"FROM otel.gateway_key_events "
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


def _gw(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{GATEWAY_URL}{path}", params=params or {},
                      headers=_gw_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        log.warning("gateway_tool_error", path=path, error=str(exc))
        return {"error": str(exc)}


def _gw_post(path: str, body: dict) -> Any:
    try:
        r = httpx.post(f"{GATEWAY_URL}{path}", json=body,
                       headers=_gw_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _gw_put(path: str, body: dict) -> Any:
    try:
        r = httpx.put(f"{GATEWAY_URL}{path}", json=body,
                      headers=_gw_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _gw_patch(path: str, body: dict) -> Any:
    try:
        r = httpx.patch(f"{GATEWAY_URL}{path}", json=body,
                        headers=_gw_headers(), timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _gw_delete(path: str) -> Any:
    try:
        r = httpx.delete(f"{GATEWAY_URL}{path}", headers=_gw_headers(), timeout=10)
        r.raise_for_status()
        return r.json() if r.content else {"status": "deleted"}
    except Exception as exc:
        return {"error": str(exc)}


# ── Tool implementations ───────────────────────────────────────────────────────

def get_gateway_call_stats(inputs: dict) -> str:
    hours = int(inputs.get("hours", 168))
    agent_role = inputs.get("agent_role", "")
    try:
        data = get_db().get_gateway_call_stats(hours=hours, agent_role=agent_role)
        return json.dumps(data, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_routing_decisions(inputs: dict) -> str:
    hours = inputs.get("hours", 168)
    agent_role = inputs.get("agent_role", "")
    limit = inputs.get("limit", 50)
    try:
        rows = get_db().get_routing_decisions(hours, agent_role, limit)
        return json.dumps({"decisions": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_gateway_proposals(inputs: dict) -> str:
    status = inputs.get("status", "pending")
    data = _gw("/gateway/changes", {"status": status}) or {}
    return json.dumps(data, default=str)


def propose_gateway_change(inputs: dict) -> str:
    change_type = inputs.get("change_type", "routing_override")
    agent_role = inputs.get("agent_role", "*")
    system_id = inputs.get("system_id", "*")
    description = inputs.get("description", "").strip()
    evidence = inputs.get("evidence", "").strip()
    proposed_by = inputs.get("proposed_by", "gateway-agent")
    payload = inputs.get("payload", {})

    if not description:
        return json.dumps({"error": "description is required"})
    if isinstance(payload, dict):
        payload = json.dumps(payload)

    result = _gw_post("/gateway/changes", {
        "change_type": change_type,
        "agent_role": agent_role,
        "system_id": system_id,
        "description": description,
        "evidence": evidence,
        "payload": payload,
        "proposed_by": proposed_by,
    })
    return json.dumps(result, default=str)


def list_ab_tests(inputs: dict) -> str:
    status = inputs.get("status", "")
    path = "/gateway/ab-tests"
    if status:
        path += f"?status={status}"
    data = _gw(path) or {}
    return json.dumps(data, default=str)


def get_ab_test_results(inputs: dict) -> str:
    test_id = inputs.get("test_id", "")
    if not test_id:
        return json.dumps({"error": "test_id is required"})
    data = _gw(f"/gateway/ab-tests/{test_id}/results") or {}
    return json.dumps(data, default=str)


def compare_shadow_vs_primary(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    hours = int(inputs.get("hours", 168))
    try:
        data = get_db().compare_shadow_vs_primary(agent_role=agent_role, hours=hours)
        return json.dumps(data, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_gateway_keys(_: dict) -> str:
    data = _gw("/gateway/keys") or {}
    return json.dumps(data, default=str)


def get_key_rejection_events(inputs: dict) -> str:
    key_id = inputs.get("key_id", "")
    hours = int(inputs.get("hours", 24))
    limit = int(inputs.get("limit", 100))
    try:
        rows = get_db().get_key_rejection_events(key_id=key_id, hours=hours, limit=limit)
        summary: dict = {}
        for r in rows:
            et = r.get("event_type", "")
            summary[et] = summary.get(et, 0) + 1
        return json.dumps({"events": rows, "count": len(rows), "by_type": summary, "hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def update_gateway_key(inputs: dict) -> str:
    key_id = inputs.get("key_id", "")
    if not key_id:
        return json.dumps({"error": "key_id is required"})
    updates = {k: v for k, v in {
        "description":       inputs.get("description"),
        "agent_role":        inputs.get("agent_role"),
        "system_id":         inputs.get("system_id"),
        "allowed_models":    inputs.get("allowed_models"),
        "daily_token_limit": inputs.get("daily_token_limit"),
        "rate_limit_rpm":    inputs.get("rate_limit_rpm"),
        "budget_alert_usd":  inputs.get("budget_alert_usd"),
        "alert_webhook_url": inputs.get("alert_webhook_url"),
    }.items() if v is not None}
    if not updates:
        return json.dumps({"error": "No fields to update — provide at least one field to change"})
    result = _gw_patch(f"/gateway/keys/{key_id}", updates)
    return json.dumps(result, default=str)


def list_routing_policies(inputs: dict) -> str:
    return json.dumps(_gw("/gateway/routing"), default=str)


def create_routing_policy(inputs: dict) -> str:
    body = {
        "agent_role":     inputs.get("agent_role", "*"),
        "system_id":      inputs.get("system_id", "*"),
        "target_model":   inputs.get("target_model", ""),
        "target_backend": inputs.get("target_backend", ""),
        "model_match":    inputs.get("model_match", ""),
        "priority":       int(inputs.get("priority", 50)),
    }
    if not body["target_model"]:
        return json.dumps({"error": "target_model is required"})
    return json.dumps(_gw_post("/gateway/routing", body), default=str)


def delete_routing_policy(inputs: dict) -> str:
    policy_id = inputs.get("policy_id", "")
    if not policy_id:
        return json.dumps({"error": "policy_id is required"})
    return json.dumps(_gw_delete(f"/gateway/routing/{policy_id}"), default=str)


def list_prompt_mods(inputs: dict) -> str:
    return json.dumps(_gw("/gateway/mods"), default=str)


def create_prompt_mod(inputs: dict) -> str:
    body = {
        "agent_role": inputs.get("agent_role", "*"),
        "system_id":  inputs.get("system_id", "*"),
        "mod_type":   inputs.get("mod_type", "system_prefix"),
        "content":    inputs.get("content", ""),
        "priority":   int(inputs.get("priority", 50)),
    }
    if not body["content"]:
        return json.dumps({"error": "content is required"})
    return json.dumps(_gw_post("/gateway/mods", body), default=str)


def delete_prompt_mod(inputs: dict) -> str:
    mod_id = inputs.get("mod_id", "")
    if not mod_id:
        return json.dumps({"error": "mod_id is required"})
    return json.dumps(_gw_delete(f"/gateway/mods/{mod_id}"), default=str)


def list_shadow_rules(inputs: dict) -> str:
    return json.dumps(_gw("/gateway/shadow"), default=str)


def create_shadow_rule(inputs: dict) -> str:
    body = {
        "agent_role":   inputs.get("agent_role", "*"),
        "system_id":    inputs.get("system_id", "*"),
        "shadow_model": inputs.get("shadow_model", ""),
        "sample_rate":  float(inputs.get("sample_rate", 1.0)),
    }
    if not body["shadow_model"]:
        return json.dumps({"error": "shadow_model is required"})
    return json.dumps(_gw_post("/gateway/shadow", body), default=str)


def delete_shadow_rule(inputs: dict) -> str:
    rule_id = inputs.get("rule_id", "")
    if not rule_id:
        return json.dumps({"error": "rule_id is required"})
    return json.dumps(_gw_delete(f"/gateway/shadow/{rule_id}"), default=str)


def create_ab_test(inputs: dict) -> str:
    body = {
        "agent_role":    inputs.get("agent_role", "*"),
        "system_id":     inputs.get("system_id", "*"),
        "name":          inputs.get("name", f"ab-test-{inputs.get('agent_role', 'unknown')}"),
        "variant_model": inputs.get("variant_model", ""),
        "traffic_split": float(inputs.get("traffic_split", 0.5)),
    }
    if not body["variant_model"]:
        return json.dumps({"error": "variant_model is required"})
    return json.dumps(_gw_post("/gateway/ab-tests", body), default=str)


def stop_ab_test(inputs: dict) -> str:
    test_id = inputs.get("test_id", "")
    if not test_id:
        return json.dumps({"error": "test_id is required"})
    return json.dumps(_gw_put(f"/gateway/ab-tests/{test_id}", {"status": "stopped"}), default=str)


def delete_ab_test(inputs: dict) -> str:
    test_id = inputs.get("test_id", "")
    if not test_id:
        return json.dumps({"error": "test_id is required"})
    return json.dumps(_gw_delete(f"/gateway/ab-tests/{test_id}"), default=str)


def create_gateway_key(inputs: dict) -> str:
    body = {
        "description":       inputs.get("description", ""),
        "agent_role":        inputs.get("agent_role", "*"),
        "system_id":         inputs.get("system_id", "*"),
        "daily_token_limit": int(inputs.get("daily_token_limit", 0)),
        "rate_limit_rpm":    int(inputs.get("rate_limit_rpm", 0)),
        "allowed_models":    inputs.get("allowed_models", []),
        "is_admin":          bool(inputs.get("is_admin", False)),
        "budget_alert_usd":  float(inputs.get("budget_alert_usd", 0.0)),
        "alert_webhook_url": inputs.get("alert_webhook_url", ""),
    }
    if not body["description"]:
        return json.dumps({"error": "description is required"})
    return json.dumps(_gw_post("/gateway/keys", body), default=str)


def revoke_gateway_key(inputs: dict) -> str:
    key_id = inputs.get("key_id", "")
    if not key_id:
        return json.dumps({"error": "key_id is required"})
    return json.dumps(_gw_delete(f"/gateway/keys/{key_id}"), default=str)


# ── Traffic management tools ───────────────────────────────────────────────────

def list_traffic_pools(_: dict) -> str:
    """List all endpoint pools with their member endpoints."""
    return json.dumps(_gw("/gateway/traffic/pools"), default=str)


def create_traffic_pool(inputs: dict) -> str:
    name = inputs.get("name", "").strip()
    strategy = inputs.get("strategy", "round_robin")
    description = inputs.get("description", "")
    valid = ("round_robin", "weighted", "least_latency", "performance", "cost_optimized", "fallback_chain")
    if not name:
        return json.dumps({"error": "name is required"})
    if strategy not in valid:
        return json.dumps({"error": f"strategy must be one of: {', '.join(valid)}"})
    return json.dumps(_gw_post("/gateway/traffic/pools", {
        "name": name, "strategy": strategy, "description": description,
    }), default=str)


def delete_traffic_pool(inputs: dict) -> str:
    pool_id = inputs.get("pool_id", "")
    if not pool_id:
        return json.dumps({"error": "pool_id is required"})
    return json.dumps(_gw_delete(f"/gateway/traffic/pools/{pool_id}"), default=str)


def add_pool_endpoint(inputs: dict) -> str:
    pool_id = inputs.get("pool_id", "")
    model = inputs.get("model", "").strip()
    if not pool_id:
        return json.dumps({"error": "pool_id is required"})
    if not model:
        return json.dumps({"error": "model is required"})
    if "," in model:
        return json.dumps({"error": "model must be a single model identifier — one endpoint per model"})
    return json.dumps(_gw_post(f"/gateway/traffic/pools/{pool_id}/endpoints", {
        "model":    model,
        "backend":  inputs.get("backend", "openai"),
        "weight":   float(inputs.get("weight", 1.0)),
        "priority": int(inputs.get("priority", 1)),
    }), default=str)


def remove_pool_endpoint(inputs: dict) -> str:
    endpoint_id = inputs.get("endpoint_id", "")
    if not endpoint_id:
        return json.dumps({"error": "endpoint_id is required"})
    return json.dumps(_gw_delete(f"/gateway/traffic/endpoints/{endpoint_id}"), default=str)


def list_traffic_policies(_: dict) -> str:
    """List all traffic policies (agent_role → pool bindings)."""
    return json.dumps(_gw("/gateway/traffic/policies"), default=str)


def create_traffic_policy(inputs: dict) -> str:
    pool_id = inputs.get("pool_id", "").strip()
    if not pool_id:
        return json.dumps({"error": "pool_id is required — call list_traffic_pools() to find it"})
    return json.dumps(_gw_post("/gateway/traffic/policies", {
        "agent_role": inputs.get("agent_role", "*"),
        "system_id":  inputs.get("system_id", "*"),
        "pool_id":    pool_id,
        "sticky":     bool(inputs.get("sticky", False)),
    }), default=str)


def delete_traffic_policy(inputs: dict) -> str:
    policy_id = inputs.get("policy_id", "")
    if not policy_id:
        return json.dumps({"error": "policy_id is required"})
    return json.dumps(_gw_delete(f"/gateway/traffic/policies/{policy_id}"), default=str)


def get_traffic_stats(inputs: dict) -> str:
    """Per-model call stats for pool-routed traffic."""
    hours = int(inputs.get("hours", 1))
    return json.dumps(_gw("/gateway/traffic/stats", {"hours": hours}), default=str)


# ── Tool dispatcher ────────────────────────────────────────────────────────────

TOOL_MAP = {
    "get_gateway_call_stats":    get_gateway_call_stats,
    "get_routing_decisions":     get_routing_decisions,
    "get_gateway_proposals":     get_gateway_proposals,
    "propose_gateway_change":    propose_gateway_change,
    "list_ab_tests":             list_ab_tests,
    "get_ab_test_results":       get_ab_test_results,
    "compare_shadow_vs_primary": compare_shadow_vs_primary,
    "get_gateway_keys":          get_gateway_keys,
    "get_key_rejection_events":  get_key_rejection_events,
    "update_gateway_key":        update_gateway_key,
    "list_routing_policies":     list_routing_policies,
    "create_routing_policy":     create_routing_policy,
    "delete_routing_policy":     delete_routing_policy,
    "list_prompt_mods":          list_prompt_mods,
    "create_prompt_mod":         create_prompt_mod,
    "delete_prompt_mod":         delete_prompt_mod,
    "list_shadow_rules":         list_shadow_rules,
    "create_shadow_rule":        create_shadow_rule,
    "delete_shadow_rule":        delete_shadow_rule,
    "create_ab_test":            create_ab_test,
    "stop_ab_test":              stop_ab_test,
    "delete_ab_test":            delete_ab_test,
    "create_gateway_key":        create_gateway_key,
    "revoke_gateway_key":        revoke_gateway_key,
    # traffic management
    "list_traffic_pools":        list_traffic_pools,
    "create_traffic_pool":       create_traffic_pool,
    "delete_traffic_pool":       delete_traffic_pool,
    "add_pool_endpoint":         add_pool_endpoint,
    "remove_pool_endpoint":      remove_pool_endpoint,
    "list_traffic_policies":     list_traffic_policies,
    "create_traffic_policy":     create_traffic_policy,
    "delete_traffic_policy":     delete_traffic_policy,
    "get_traffic_stats":         get_traffic_stats,
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
        "name": "get_gateway_call_stats",
        "description": (
            "Aggregate gateway call statistics per (agent_role, model_used) with eval scores. "
            "Returns call_stats (call count, avg latency, total tokens, error count) and "
            "eval_scores_by_model (faithfulness, relevance, task_success_rate per agent+model). "
            "Use to compare model quality and cost across routing policies."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours":      {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"},
                "agent_role": {"type": "string",  "description": "Filter to a single agent role. Empty = all roles."},
            },
        },
    },
    {
        "name": "get_routing_decisions",
        "description": "Get model routing decisions: which complexity tier was chosen, which model was selected, and estimated cost savings.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"},
                "agent_role": {"type": "string", "description": "Filter by agent role"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    {
        "name": "get_gateway_proposals",
        "description": (
            "Read the gateway change proposal queue — proposals awaiting review or already decided. "
            "Call before proposing a new change to avoid duplicates."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["pending", "approved", "rejected", ""],
                    "description": "Filter by status. Empty = all. Default = pending.",
                },
            },
        },
    },
    {
        "name": "propose_gateway_change",
        "description": (
            "Submit a change proposal to the gateway for human operator review. "
            "Always call get_gateway_call_stats() first to gather evidence, then "
            "get_gateway_proposals(status='pending') to confirm no duplicate is pending."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "change_type": {
                    "type": "string",
                    "enum": [
                        "routing_override", "prompt_modification", "shadow_rule",
                        "model_downgrade", "model_upgrade", "prompt_prefix_add",
                        "few_shot_add", "other",
                    ],
                    "description": "Type of change being proposed.",
                },
                "agent_role":  {"type": "string", "description": "Agent role the change targets. '*' = all roles."},
                "system_id":   {"type": "string", "description": "System ID to target. '*' = all systems."},
                "description": {"type": "string", "description": "REQUIRED. Clear explanation of what is being changed and why."},
                "evidence":    {"type": "string", "description": "Quantitative evidence: score deltas, call counts, cost savings."},
                "payload":     {
                    "type": "object",
                    "description": "Change-specific parameters. For routing_override: {target_model}. For shadow_rule: {shadow_model, sample_rate}.",
                },
                "proposed_by": {"type": "string", "description": "Identifier of the proposer (default: 'gateway-agent')."},
            },
            "required": ["change_type", "description"],
        },
    },
    {
        "name": "list_ab_tests",
        "description": "List A/B tests from the gateway. Returns test configurations including variant models, split ratio, agent_role scope, and status.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {
                    "type": "string",
                    "enum": ["running", "paused", "completed", "draft", ""],
                    "description": "Filter by status. Empty = all tests.",
                },
            },
        },
    },
    {
        "name": "get_ab_test_results",
        "description": "Get per-variant results for a specific A/B test: call stats and eval scores per variant.",
        "input_schema": {
            "type": "object",
            "properties": {
                "test_id": {"type": "string", "description": "The test_id from list_ab_tests()"},
            },
            "required": ["test_id"],
        },
    },
    {
        "name": "compare_shadow_vs_primary",
        "description": "Compare shadow model quality against the primary model for an agent role. Returns shadow call volume, shadow scores, and primary scores side by side.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Agent role to analyse. Empty = all roles."},
                "hours": {"type": "integer", "description": "Lookback window in hours. Default 168 (7 days)."},
            },
        },
    },
    {
        "name": "get_gateway_keys",
        "description": "List all active virtual gateway API keys with their full configuration: key_id, prefix, description, agent_role binding, allowed_models, limits, and today's usage.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_key_rejection_events",
        "description": "Retrieve gateway key rejection and block events — every 401, 403, and 429 the gateway returned. Event types: invalid_key, missing_key, key_revoked, role_mismatch, model_blocked, token_limit, rate_limit.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key_id":  {"type": "string",  "description": "Filter to a specific key_id. Empty = all keys."},
                "hours":   {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "limit":   {"type": "integer", "description": "Max events to return (default 100)"},
            },
        },
    },
    {
        "name": "update_gateway_key",
        "description": "Update the configuration of an existing virtual gateway API key. Only supply fields to change. Always call get_gateway_keys() first to confirm the key_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "key_id":            {"type": "string",  "description": "REQUIRED. The key_id from get_gateway_keys()."},
                "description":       {"type": "string"},
                "agent_role":        {"type": "string"},
                "system_id":         {"type": "string"},
                "allowed_models":    {"type": "array", "items": {"type": "string"}},
                "daily_token_limit": {"type": "integer"},
                "rate_limit_rpm":    {"type": "integer"},
                "budget_alert_usd":  {"type": "number"},
                "alert_webhook_url": {"type": "string"},
            },
            "required": ["key_id"],
        },
    },
    {
        "name": "list_routing_policies",
        "description": "List all routing policies currently configured on the gateway, with policy_id, agent_role, target_model, priority, and enabled status. Always call this before delete_routing_policy.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_routing_policy",
        "description": "Create a routing policy on the gateway to direct an agent role to a specific model immediately (no operator approval required).",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role":     {"type": "string", "description": "Agent role to target. '*' = all roles."},
                "target_model":   {"type": "string", "description": "REQUIRED. Model string e.g. 'anthropic/claude-haiku-4-5-20251001'"},
                "system_id":      {"type": "string", "description": "System ID scope. Default '*'"},
                "target_backend": {"type": "string"},
                "model_match":    {"type": "string"},
                "priority":       {"type": "integer", "description": "Priority 1-100. Default 50."},
            },
            "required": ["agent_role", "target_model"],
        },
    },
    {
        "name": "delete_routing_policy",
        "description": "Disable/delete a routing policy by its policy_id. Always call list_routing_policies first to find the correct policy_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {"policy_id": {"type": "string", "description": "REQUIRED. The policy_id to delete."}},
            "required": ["policy_id"],
        },
    },
    {
        "name": "list_prompt_mods",
        "description": "List all active prompt modification rules on the gateway, with mod_id, agent_role, mod_type, content, and priority. Always call this before delete_prompt_mod.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_prompt_mod",
        "description": "Create a prompt modification rule that injects a system prefix, suffix, or few-shot example for an agent role.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Agent role to target. '*' = all."},
                "mod_type":   {"type": "string", "enum": ["system_prefix", "system_suffix", "few_shot_example"]},
                "content":    {"type": "string", "description": "REQUIRED. The text to inject."},
                "system_id":  {"type": "string"},
                "priority":   {"type": "integer"},
            },
            "required": ["agent_role", "mod_type", "content"],
        },
    },
    {
        "name": "delete_prompt_mod",
        "description": "Delete a prompt modification rule by mod_id. Always call list_prompt_mods first to find the correct mod_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {"mod_id": {"type": "string", "description": "REQUIRED. The mod_id to delete."}},
            "required": ["mod_id"],
        },
    },
    {
        "name": "list_shadow_rules",
        "description": "List all active shadow rules on the gateway, with rule_id, agent_role, shadow_model, sample_rate, and enabled status. Always call this before delete_shadow_rule.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_shadow_rule",
        "description": "Create a shadow rule that duplicates agent traffic to a secondary model for comparison. Runs in parallel without affecting the primary response.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role":   {"type": "string", "description": "Agent role to shadow. '*' = all."},
                "shadow_model": {"type": "string", "description": "REQUIRED. Shadow model string."},
                "system_id":    {"type": "string"},
                "sample_rate":  {"type": "number", "description": "Fraction of traffic to shadow (0.0-1.0). Default 1.0"},
            },
            "required": ["agent_role", "shadow_model"],
        },
    },
    {
        "name": "delete_shadow_rule",
        "description": "Delete a shadow rule by rule_id. Always call list_shadow_rules first to find the correct rule_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {"rule_id": {"type": "string", "description": "REQUIRED. The rule_id to delete."}},
            "required": ["rule_id"],
        },
    },
    {
        "name": "create_ab_test",
        "description": "Create an A/B test that splits traffic between the current primary model and a variant model.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role":    {"type": "string", "description": "Agent role to test. '*' = all."},
                "variant_model": {"type": "string", "description": "REQUIRED. The variant/challenger model."},
                "system_id":     {"type": "string"},
                "traffic_split": {"type": "number", "description": "Fraction of traffic to variant (0.0-1.0). Default 0.5"},
                "name":          {"type": "string", "description": "Human-readable test name."},
            },
            "required": ["agent_role", "variant_model"],
        },
    },
    {
        "name": "stop_ab_test",
        "description": "Pause a running A/B test without deleting it. Use when you have enough data to make a routing decision.",
        "input_schema": {
            "type": "object",
            "properties": {"test_id": {"type": "string", "description": "REQUIRED. The test_id from list_ab_tests()."}},
            "required": ["test_id"],
        },
    },
    {
        "name": "delete_ab_test",
        "description": "Permanently delete an A/B test by test_id. Always call list_ab_tests first to find the correct test_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {"test_id": {"type": "string", "description": "REQUIRED. The test_id to delete."}},
            "required": ["test_id"],
        },
    },
    {
        "name": "create_gateway_key",
        "description": "Issue a new virtual gateway API key for an agent or system. The full key is returned once — remind the user to save it immediately.",
        "input_schema": {
            "type": "object",
            "properties": {
                "description":       {"type": "string",  "description": "REQUIRED. Human-readable purpose of this key."},
                "agent_role":        {"type": "string"},
                "system_id":         {"type": "string"},
                "daily_token_limit": {"type": "integer"},
                "rate_limit_rpm":    {"type": "integer"},
                "allowed_models":    {"type": "array", "items": {"type": "string"}},
                "is_admin":          {"type": "boolean"},
                "budget_alert_usd":  {"type": "number"},
                "alert_webhook_url": {"type": "string"},
            },
            "required": ["description"],
        },
    },
    {
        "name": "revoke_gateway_key",
        "description": "Immediately revoke a gateway API key. Any agent using this key will receive 401 errors within 60 seconds. Always call get_gateway_keys first to find the correct key_id — never ask the user for it.",
        "input_schema": {
            "type": "object",
            "properties": {"key_id": {"type": "string", "description": "REQUIRED. The key_id from get_gateway_keys()."}},
            "required": ["key_id"],
        },
    },
    # ── Traffic management ─────────────────────────────────────────────────────
    {
        "name": "list_traffic_pools",
        "description": (
            "List all endpoint pools with their member endpoints. "
            "Returns pool_id, name, strategy, description, enabled flag, and the list of endpoints "
            "(endpoint_id, model, backend, weight, priority). "
            "Always call this before add_pool_endpoint, delete_traffic_pool, or create_traffic_policy."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_traffic_pool",
        "description": (
            "Create a named endpoint pool with a load-balancing strategy. "
            "After creating the pool, call add_pool_endpoint for each model in the pool, "
            "then create_traffic_policy to bind the pool to an agent role. "
            "Strategies: round_robin (even rotation), weighted (by weight field), "
            "least_latency (lowest avg latency wins), performance (best eval score), "
            "cost_optimized (cheapest model first), fallback_chain (by priority order)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "name":        {"type": "string", "description": "REQUIRED. Human-readable pool name."},
                "strategy":    {
                    "type": "string",
                    "enum": ["round_robin", "weighted", "least_latency", "performance", "cost_optimized", "fallback_chain"],
                    "description": "Load-balancing strategy. Default: round_robin.",
                },
                "description": {"type": "string", "description": "Optional description of the pool's purpose."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "delete_traffic_pool",
        "description": (
            "Disable an endpoint pool by pool_id. "
            "This also stops any traffic policies that reference this pool. "
            "Always call list_traffic_pools first to confirm the pool_id — never ask the user for it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"pool_id": {"type": "string", "description": "REQUIRED. The pool_id from list_traffic_pools()."}},
            "required": ["pool_id"],
        },
    },
    {
        "name": "add_pool_endpoint",
        "description": (
            "Add a single LLM endpoint to a pool. Call once per model. "
            "weight: relative traffic share for 'weighted' strategy (e.g. 2.0 = double traffic). "
            "priority: order for 'fallback_chain' strategy (1 = try first). "
            "backend: 'openai', 'anthropic', 'ollama', 'azure', etc."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pool_id":   {"type": "string",  "description": "REQUIRED. The pool_id from list_traffic_pools() or the just-created pool."},
                "model":     {"type": "string",  "description": "REQUIRED. Single model identifier, e.g. 'gpt-4o-mini' or 'anthropic/claude-haiku-4-5-20251001'."},
                "backend":   {"type": "string",  "description": "Provider backend. Default: 'openai'."},
                "weight":    {"type": "number",  "description": "Relative weight for weighted strategy. Default: 1.0."},
                "priority":  {"type": "integer", "description": "Priority order for fallback_chain strategy. 1 = highest priority. Default: 1."},
            },
            "required": ["pool_id", "model"],
        },
    },
    {
        "name": "remove_pool_endpoint",
        "description": (
            "Remove a single endpoint from a pool by endpoint_id. "
            "Always call list_traffic_pools first to find the endpoint_id — never ask the user for it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"endpoint_id": {"type": "string", "description": "REQUIRED. The endpoint_id from list_traffic_pools()."}},
            "required": ["endpoint_id"],
        },
    },
    {
        "name": "list_traffic_policies",
        "description": (
            "List all traffic policies — the bindings that map an agent_role (and optionally system_id) "
            "to an endpoint pool. Returns policy_id, agent_role, system_id, pool_id, sticky flag. "
            "Always call this before delete_traffic_policy."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_traffic_policy",
        "description": (
            "Bind an agent_role to an endpoint pool so all its LLM calls are load-balanced across the pool. "
            "Call list_traffic_pools first to confirm the pool_id. "
            "sticky=true pins a conversation to the same endpoint for its full duration (useful for context continuity). "
            "Traffic policy takes priority over routing policies and A/B tests for the same agent_role."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pool_id":    {"type": "string",  "description": "REQUIRED. Pool to route to. Get from list_traffic_pools()."},
                "agent_role": {"type": "string",  "description": "Agent role to bind. '*' = all roles. Default: '*'."},
                "system_id":  {"type": "string",  "description": "System scope. '*' = all systems. Default: '*'."},
                "sticky":     {"type": "boolean", "description": "Pin each conversation to the same endpoint. Default: false."},
            },
            "required": ["pool_id"],
        },
    },
    {
        "name": "delete_traffic_policy",
        "description": (
            "Remove a traffic policy by policy_id, unbinding the agent_role from its pool. "
            "Traffic falls back to routing policies and A/B tests. "
            "Always call list_traffic_policies first to find the policy_id — never ask the user for it."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"policy_id": {"type": "string", "description": "REQUIRED. The policy_id from list_traffic_policies()."}},
            "required": ["policy_id"],
        },
    },
    {
        "name": "get_traffic_stats",
        "description": (
            "Get live call statistics for pool-routed traffic: calls, avg latency, error rate, and total tokens "
            "grouped by (model_used, routing_reason). routing_reason encodes pool_id and endpoint_id prefixes "
            "so you can see which endpoints in which pools are receiving traffic. "
            "Use to detect imbalances, high error rates, or latency outliers across pool endpoints."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours. Default: 1."},
            },
        },
    },
]
