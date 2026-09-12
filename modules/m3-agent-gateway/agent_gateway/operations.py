"""Operations surface — /gateway/* endpoints for operators.

Provides visibility into gateway state and control over policies.
All reads return cached data from ChangeStore.
All writes go to ClickHouse and trigger an immediate cache refresh.

Endpoints:
  GET  /gateway/status           — live summary: call stats + policy counts
  GET  /gateway/calls            — recent call log (filterable)
  GET  /gateway/routing          — active routing policies
  POST /gateway/routing          — create routing policy
  DEL  /gateway/routing/{id}     — disable routing policy
  GET  /gateway/mods             — active prompt modifications
  POST /gateway/mods             — create prompt modification
  DEL  /gateway/mods/{id}        — disable prompt modification
  GET  /gateway/shadow           — active shadow rules
  POST /gateway/shadow           — create shadow rule
  DEL  /gateway/shadow/{id}      — disable shadow rule
  GET  /gateway/ab-tests         — list A/B tests
  POST /gateway/ab-tests         — create A/B test
  PUT  /gateway/ab-tests/{id}    — update A/B test status (running/paused/completed)
  DEL  /gateway/ab-tests/{id}    — delete A/B test
  GET  /gateway/ab-tests/{id}/results — per-variant call stats and eval scores
  GET  /gateway/changes          — proposed changes (filterable by status)
  POST /gateway/changes          — propose a new change
  PUT  /gateway/changes/{id}     — approve or reject a proposed change
  GET  /gateway/traffic/pools                    — list endpoint pools (with members)
  POST /gateway/traffic/pools                    — create pool
  DEL  /gateway/traffic/pools/{id}               — disable pool
  POST /gateway/traffic/pools/{id}/endpoints     — add endpoint to pool
  DEL  /gateway/traffic/endpoints/{id}           — remove endpoint from pool
  GET  /gateway/traffic/policies                 — list traffic policies
  POST /gateway/traffic/policies                 — create traffic policy
  DEL  /gateway/traffic/policies/{id}            — disable traffic policy
  GET  /gateway/traffic/stats                    — per-model call stats for pool endpoints
"""

import asyncio
import hashlib
import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import proxy as _proxy_module

_MASTER_KEY = os.getenv("GATEWAY_MASTER_KEY", "")

log = logging.getLogger("gateway.operations")

router = APIRouter()

# Injected at startup via set_deps()
_db    = None
_store = None


def set_deps(db, store) -> None:
    global _db, _store
    _db    = db
    _store = store


async def _require_admin(request: Request) -> None:
    """FastAPI dependency — enforces admin key on write endpoints.

    If GATEWAY_MASTER_KEY is not set the check is skipped (dev/open mode).
    Accepts the key via X-Gateway-Admin-Key header or Authorization: Bearer.
    """
    if not _MASTER_KEY:
        return  # no master key configured → open mode
    key = (
        request.headers.get("x-gateway-admin-key", "")
        or request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    )
    if not key:
        raise HTTPException(status_code=403,
                            detail="Admin key required. Pass X-Gateway-Admin-Key header.")
    if key == _MASTER_KEY:
        return
    kh = hashlib.sha256(key.encode()).hexdigest()
    record = _db.validate_api_key(kh)
    if record and record.get("is_admin") and record.get("enabled"):
        return
    raise HTTPException(status_code=403, detail="Invalid or non-admin key.")


# ── Status ────────────────────────────────────────────────────────────────────

@router.get("/status")
async def gateway_status(hours: int = 24):
    stats      = await asyncio.to_thread(_db.get_call_stats, hours)
    keys       = await asyncio.to_thread(_db.list_api_keys)
    cache_info        = _proxy_module.cache_stats()
    sem_cache_info    = _proxy_module.semantic_cache_stats()
    return {
        "status": "ok",
        "policy_counts": {
            "routing_policies": len(_store.routing),
            "prompt_mods":      len(_store.mods),
            "shadow_rules":     len(_store.shadow),
            "ab_tests_running": len(_store.ab_tests),
            "api_keys_active":  len(keys),
        },
        "call_stats_24h": stats,
        "stats_hours": hours,
        "cache": cache_info,
        "semantic_cache": sem_cache_info,
        "auth": {
            "enabled":    bool(os.getenv("GATEWAY_AUTH_ENABLED", "false").lower() == "true"),
            "master_key": bool(os.getenv("GATEWAY_MASTER_KEY", "")),
        },
    }


# ── Call log ──────────────────────────────────────────────────────────────────

@router.get("/calls")
async def get_calls(
    limit:      int = 200,
    hours:      int = 24,
    system_id:  str = "",
    agent_role: str = "",
):
    calls = await asyncio.to_thread(_db.get_call_log, limit, system_id, agent_role, hours)
    return {"calls": calls, "count": len(calls)}


# ── Routing policies ──────────────────────────────────────────────────────────

class RoutingPolicyIn(BaseModel):
    agent_role:      str = "*"
    system_id:       str = "*"
    model_match:     str = ""
    target_model:    str
    target_backend:  str = "openai"
    reason:          str = ""
    fallback_model:  str = ""
    fallback_backend: str = "openai"


@router.get("/routing")
async def get_routing():
    return {"policies": _store.routing, "count": len(_store.routing)}


@router.post("/routing", dependencies=[Depends(_require_admin)])
async def create_routing_policy(req: RoutingPolicyIn):
    await asyncio.to_thread(
        _db.insert_routing_policy,
        req.agent_role, req.system_id, req.model_match,
        req.target_model, req.target_backend, req.reason,
        req.fallback_model, req.fallback_backend,
    )
    await _store._refresh()
    return {"status": "created"}


class UpdateRoutingPolicyIn(BaseModel):
    model_match:      str = ""
    target_model:     str
    target_backend:   str = "openai"
    reason:           str = ""
    fallback_model:   str = ""
    fallback_backend: str = "openai"


@router.put("/routing/{policy_id}", dependencies=[Depends(_require_admin)])
async def update_routing_policy(policy_id: str, req: UpdateRoutingPolicyIn):
    if not req.target_model.strip():
        raise HTTPException(status_code=400, detail="target_model is required")
    await asyncio.to_thread(
        _db.update_routing_policy,
        policy_id, req.model_match, req.target_model.strip(),
        req.target_backend, req.reason,
        req.fallback_model, req.fallback_backend,
    )
    await _store._refresh()
    return {"status": "updated", "policy_id": policy_id}


@router.delete("/routing/{policy_id}", dependencies=[Depends(_require_admin)])
async def disable_routing_policy(policy_id: str):
    await asyncio.to_thread(_db.disable_routing_policy, policy_id)
    await _store._refresh()
    return {"status": "disabled", "policy_id": policy_id}


# ── Prompt modifications ──────────────────────────────────────────────────────

class PromptModIn(BaseModel):
    agent_role:     str   = "*"
    system_id:      str   = "*"
    mod_type:       str   = "system_prefix"  # system_prefix | system_suffix | few_shot
    content:        str
    evidence_delta: float = 0.0


@router.get("/mods")
async def get_mods():
    return {"mods": _store.mods, "count": len(_store.mods)}


@router.post("/mods", dependencies=[Depends(_require_admin)])
async def create_prompt_mod(req: PromptModIn):
    if req.mod_type not in ("system_prefix", "system_suffix", "few_shot"):
        raise HTTPException(
            status_code=400,
            detail="mod_type must be system_prefix | system_suffix | few_shot",
        )
    await asyncio.to_thread(
        _db.insert_prompt_mod,
        req.agent_role, req.system_id, req.mod_type,
        req.content, req.evidence_delta,
    )
    await _store._refresh()
    return {"status": "created"}


class UpdatePromptModIn(BaseModel):
    mod_type:       str   = "system_prefix"
    content:        str
    evidence_delta: float = 0.0


@router.put("/mods/{mod_id}", dependencies=[Depends(_require_admin)])
async def update_prompt_mod(mod_id: str, req: UpdatePromptModIn):
    if req.mod_type not in ("system_prefix", "system_suffix", "few_shot"):
        raise HTTPException(status_code=400, detail="mod_type must be system_prefix | system_suffix | few_shot")
    if not req.content.strip():
        raise HTTPException(status_code=400, detail="content is required")
    await asyncio.to_thread(
        _db.update_prompt_mod,
        mod_id, req.mod_type, req.content, req.evidence_delta,
    )
    await _store._refresh()
    return {"status": "updated", "mod_id": mod_id}


@router.delete("/mods/{mod_id}", dependencies=[Depends(_require_admin)])
async def disable_prompt_mod(mod_id: str):
    await asyncio.to_thread(_db.disable_prompt_mod, mod_id)
    await _store._refresh()
    return {"status": "disabled", "mod_id": mod_id}


# ── Shadow rules ──────────────────────────────────────────────────────────────

class ShadowRuleIn(BaseModel):
    agent_role:     str   = "*"
    system_id:      str   = "*"
    shadow_model:   str
    shadow_backend: str   = "openai"
    sample_rate:    float = 0.1


@router.get("/shadow")
async def get_shadow():
    return {"rules": _store.shadow, "count": len(_store.shadow)}


@router.post("/shadow", dependencies=[Depends(_require_admin)])
async def create_shadow_rule(req: ShadowRuleIn):
    if not 0.0 < req.sample_rate <= 1.0:
        raise HTTPException(status_code=400, detail="sample_rate must be in (0, 1]")
    await asyncio.to_thread(
        _db.insert_shadow_rule,
        req.agent_role, req.system_id, req.shadow_model,
        req.shadow_backend, req.sample_rate,
    )
    await _store._refresh()
    return {"status": "created"}


class UpdateShadowRuleIn(BaseModel):
    shadow_model:   str
    shadow_backend: str   = "openai"
    sample_rate:    float = 0.1


@router.put("/shadow/{rule_id}", dependencies=[Depends(_require_admin)])
async def update_shadow_rule(rule_id: str, req: UpdateShadowRuleIn):
    if not req.shadow_model.strip():
        raise HTTPException(status_code=400, detail="shadow_model is required")
    if not 0.0 < req.sample_rate <= 1.0:
        raise HTTPException(status_code=400, detail="sample_rate must be in (0, 1]")
    await asyncio.to_thread(
        _db.update_shadow_rule,
        rule_id, req.shadow_model.strip(), req.shadow_backend, req.sample_rate,
    )
    await _store._refresh()
    return {"status": "updated", "rule_id": rule_id}


@router.delete("/shadow/{rule_id}", dependencies=[Depends(_require_admin)])
async def disable_shadow_rule(rule_id: str):
    await asyncio.to_thread(_db.disable_shadow_rule, rule_id)
    await _store._refresh()
    return {"status": "disabled", "rule_id": rule_id}


# ── Pipeline shadow evals ─────────────────────────────────────────────────────

class PipelineShadowEvalIn(BaseModel):
    agent_role:           str   = ""
    user_input:           str   = ""
    primary_model:        str   = ""
    shadow_model:         str   = ""
    primary_response:     str   = ""
    shadow_response:      str   = ""
    primary_tokens:       int   = 0
    shadow_tokens:        int   = 0
    primary_latency_ms:   int   = 0
    shadow_latency_ms:    int   = 0
    primary_faithfulness: float = 0.0
    primary_relevance:    float = 0.0
    primary_instruction:  float = 0.0
    shadow_faithfulness:  float = 0.0
    shadow_relevance:     float = 0.0
    shadow_instruction:   float = 0.0


@router.post("/pipeline-shadow-evals")
async def log_pipeline_shadow_eval(req: PipelineShadowEvalIn):
    await asyncio.to_thread(_db.log_pipeline_shadow_eval, req.model_dump())
    return {"status": "logged"}


@router.get("/pipeline-shadow-evals")
async def get_pipeline_shadow_evals(
    agent_role: str = "",
    hours:      int  = 168,
    limit:      int  = 100,
    summary:    bool = False,
):
    if summary:
        data = await asyncio.to_thread(_db.get_pipeline_shadow_summary, agent_role, hours)
        return {"summary": data, "count": len(data), "hours": hours}
    data = await asyncio.to_thread(_db.get_pipeline_shadow_evals, agent_role, hours, limit)
    return {"evals": data, "count": len(data), "hours": hours}


# ── A/B tests ─────────────────────────────────────────────────────────────────

class ABTestIn(BaseModel):
    test_name:         str
    agent_role:        str   = "*"
    system_id:         str   = "*"
    variant_a_model:   str   = ""
    variant_a_backend: str   = "openai"
    variant_a_prompt:  str   = ""
    variant_b_model:   str   = ""
    variant_b_backend: str   = "openai"
    variant_b_prompt:  str   = ""
    split_ratio:       float = 0.5


class ABTestStatusIn(BaseModel):
    status: str  # running | paused | completed


@router.get("/ab-tests")
async def list_ab_tests(status: str = ""):
    tests = await asyncio.to_thread(_db.get_ab_tests, status)
    return {"tests": tests, "count": len(tests)}


@router.post("/ab-tests", dependencies=[Depends(_require_admin)])
async def create_ab_test(req: ABTestIn):
    if not 0.0 < req.split_ratio < 1.0:
        raise HTTPException(status_code=400, detail="split_ratio must be between 0 and 1 exclusive")
    if not req.test_name.strip():
        raise HTTPException(status_code=400, detail="test_name is required")
    test_id = await asyncio.to_thread(
        _db.insert_ab_test,
        req.test_name, req.agent_role, req.system_id,
        req.variant_a_model, req.variant_a_backend, req.variant_a_prompt,
        req.variant_b_model, req.variant_b_backend, req.variant_b_prompt,
        req.split_ratio,
    )
    await _store._refresh()
    return {"test_id": test_id, "status": "draft"}


@router.put("/ab-tests/{test_id}", dependencies=[Depends(_require_admin)])
async def update_ab_test_status(test_id: str, req: ABTestStatusIn):
    valid = {"running", "paused", "completed", "draft"}
    if req.status not in valid:
        raise HTTPException(status_code=400, detail=f"status must be one of {valid}")
    await asyncio.to_thread(_db.update_ab_test_status, test_id, req.status)
    await _store._refresh()
    return {"test_id": test_id, "status": req.status}


@router.delete("/ab-tests/{test_id}", dependencies=[Depends(_require_admin)])
async def delete_ab_test(test_id: str):
    await asyncio.to_thread(_db.delete_ab_test, test_id)
    await _store._refresh()
    return {"test_id": test_id, "status": "deleted"}


@router.get("/ab-tests/{test_id}/results")
async def get_ab_test_results(test_id: str):
    results = await asyncio.to_thread(_db.get_ab_test_results, test_id)
    return results


# ── API key management ────────────────────────────────────────────────────────

class APIKeyIn(BaseModel):
    description:       str
    agent_role:        str   = "*"
    system_id:         str   = "*"
    allowed_models:    list  = []
    daily_token_limit: int   = 0
    is_admin:          bool  = False
    rate_limit_rpm:    int   = 0
    budget_alert_usd:  float = 0.0
    alert_webhook_url: str   = ""


class UpdateAPIKeyIn(BaseModel):
    description:       Optional[str]   = None
    agent_role:        Optional[str]   = None
    system_id:         Optional[str]   = None
    allowed_models:    Optional[list]  = None
    daily_token_limit: Optional[int]   = None
    rate_limit_rpm:    Optional[int]   = None
    budget_alert_usd:  Optional[float] = None
    alert_webhook_url: Optional[str]   = None


@router.get("/keys", dependencies=[Depends(_require_admin)])
async def list_keys():
    keys  = await asyncio.to_thread(_db.list_api_keys)
    usage = await asyncio.to_thread(_db.get_key_usage)
    usage_map = {u["key_id"]: u for u in usage}
    for k in keys:
        u = usage_map.get(k["key_id"], {})
        k["calls_today"]  = int(u.get("calls_today",  0))
        k["tokens_today"] = int(u.get("tokens_today", 0))
    return {"keys": keys, "count": len(keys)}


@router.post("/keys", dependencies=[Depends(_require_admin)])
async def create_key(req: APIKeyIn):
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="description is required")
    key_id, raw_key = await asyncio.to_thread(
        _db.create_api_key,
        req.description, req.agent_role, req.system_id,
        req.allowed_models, req.daily_token_limit, req.is_admin,
        req.rate_limit_rpm, req.budget_alert_usd, req.alert_webhook_url,
    )
    return {
        "key_id":  key_id,
        "key":     raw_key,
        "message": "Save this key — it will not be shown again.",
    }


@router.patch("/keys/{key_id}", dependencies=[Depends(_require_admin)])
async def update_key(key_id: str, req: UpdateAPIKeyIn):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if not updates:
        raise HTTPException(status_code=400, detail="No fields to update")
    ok = await asyncio.to_thread(_db.update_api_key, key_id, updates)
    if not ok:
        raise HTTPException(status_code=404, detail="Key not found or already revoked")
    return {"key_id": key_id, "status": "updated", "updated_fields": list(updates.keys())}


@router.delete("/keys/{key_id}", dependencies=[Depends(_require_admin)])
async def revoke_key(key_id: str):
    await asyncio.to_thread(_db.disable_api_key, key_id)
    return {"key_id": key_id, "status": "revoked"}


# ── Cache management ─────────────────────────────────────────────────────────

@router.get("/cache")
async def get_cache_stats():
    return _proxy_module.cache_stats()


@router.get("/cache/entries")
async def get_cache_entries():
    return {"entries": _proxy_module.cache_entries()}


@router.get("/cache/all-entries")
async def get_all_cache_entries():
    entries = _proxy_module.cache_entries() + _proxy_module.semantic_cache_entries()
    entries.sort(key=lambda e: e["ttl_remaining_s"], reverse=True)
    return {"entries": entries}


@router.delete("/cache", dependencies=[Depends(_require_admin)])
async def flush_cache():
    n = _proxy_module.cache_flush()
    return {"status": "flushed", "entries_removed": n}


@router.get("/semantic-cache")
async def get_semantic_cache_stats():
    return _proxy_module.semantic_cache_stats()


@router.get("/semantic-cache/entries")
async def get_semantic_cache_entries():
    return {"entries": _proxy_module.semantic_cache_entries()}


@router.delete("/semantic-cache", dependencies=[Depends(_require_admin)])
async def flush_semantic_cache():
    n = _proxy_module.semantic_cache_flush()
    return {"status": "flushed", "entries_removed": n}


# ── Proposed changes ──────────────────────────────────────────────────────────

class ProposeChangeIn(BaseModel):
    change_type:  str
    agent_role:   str = "*"
    system_id:    str = "*"
    description:  str
    payload:      str = "{}"
    evidence:     str = ""
    proposed_by:  str = "api"


class DecideChangeIn(BaseModel):
    status:      str   # approved | rejected
    approved_by: str = "operator"


@router.get("/changes")
async def get_changes(status: str = "pending"):
    changes = await asyncio.to_thread(_db.get_proposed_changes, status)
    return {"changes": changes, "count": len(changes)}


@router.post("/changes", dependencies=[Depends(_require_admin)])
async def propose_change(req: ProposeChangeIn):
    change_id = await asyncio.to_thread(
        _db.propose_change,
        req.change_type, req.agent_role, req.system_id,
        req.description, req.payload, req.evidence, req.proposed_by,
    )
    return {"change_id": change_id, "status": "pending"}


@router.put("/changes/{change_id}", dependencies=[Depends(_require_admin)])
async def decide_change(change_id: str, req: DecideChangeIn):
    if req.status not in ("approved", "rejected"):
        raise HTTPException(
            status_code=400,
            detail="status must be 'approved' or 'rejected'",
        )
    await asyncio.to_thread(_db.decide_change, change_id, req.status, req.approved_by)
    applied: Optional[dict] = None
    if req.status == "approved":
        applied = await asyncio.to_thread(_db.auto_apply_change, change_id)
        await _store._refresh()
    return {"change_id": change_id, "status": req.status, "applied": applied}


# ── Traffic management — endpoint pools ──────────────────────────────────────

_VALID_STRATEGIES = ("round_robin", "weighted", "least_latency",
                     "performance", "cost_optimized", "fallback_chain")


class EndpointPoolIn(BaseModel):
    name:        str
    strategy:    str   = "round_robin"
    description: str   = ""


class PoolEndpointIn(BaseModel):
    model:    str
    backend:  str   = "openai"
    weight:   float = 1.0
    priority: int   = 1


class TrafficPolicyIn(BaseModel):
    agent_role: str  = "*"
    system_id:  str  = "*"
    pool_id:    str
    sticky:     bool = False


@router.get("/traffic/pools")
async def list_pools():
    pools     = _store.pools
    endpoints = _store.pool_endpoints
    eps_by_pool: dict[str, list] = {}
    for ep in endpoints:
        eps_by_pool.setdefault(ep["pool_id"], []).append(ep)
    result = [
        {**p, "endpoints": eps_by_pool.get(p["pool_id"], [])}
        for p in pools
    ]
    return {"pools": result, "count": len(result)}


@router.post("/traffic/pools", dependencies=[Depends(_require_admin)])
async def create_pool(req: EndpointPoolIn):
    if req.strategy not in _VALID_STRATEGIES:
        raise HTTPException(
            status_code=400,
            detail=f"strategy must be one of: {', '.join(_VALID_STRATEGIES)}",
        )
    pool_id = await asyncio.to_thread(
        _db.insert_endpoint_pool, req.name, req.strategy, req.description
    )
    await _store._refresh_traffic()
    return {"pool_id": pool_id, "status": "created"}


@router.delete("/traffic/pools/{pool_id}", dependencies=[Depends(_require_admin)])
async def delete_pool(pool_id: str):
    await asyncio.to_thread(_db.disable_endpoint_pool, pool_id)
    await _store._refresh_traffic()
    return {"pool_id": pool_id, "status": "disabled"}


@router.post("/traffic/pools/{pool_id}/endpoints", dependencies=[Depends(_require_admin)])
async def add_pool_endpoint(pool_id: str, req: PoolEndpointIn):
    if not req.model.strip():
        raise HTTPException(status_code=400, detail="model is required")
    if "," in req.model:
        raise HTTPException(status_code=400,
                            detail="model must be a single model identifier. "
                                   "Add one endpoint per model.")
    if req.weight < 0:
        raise HTTPException(status_code=400, detail="weight must be >= 0")
    endpoint_id = await asyncio.to_thread(
        _db.insert_pool_endpoint,
        pool_id, req.model.strip(), req.backend, req.weight, req.priority,
    )
    await _store._refresh_traffic()
    return {"endpoint_id": endpoint_id, "status": "created"}


@router.delete("/traffic/endpoints/{endpoint_id}", dependencies=[Depends(_require_admin)])
async def remove_pool_endpoint(endpoint_id: str):
    await asyncio.to_thread(_db.disable_pool_endpoint, endpoint_id)
    await _store._refresh_traffic()
    return {"endpoint_id": endpoint_id, "status": "disabled"}


# ── Traffic management — policies ─────────────────────────────────────────────

@router.get("/traffic/policies")
async def list_traffic_policies():
    return {"policies": _store.traffic_policies, "count": len(_store.traffic_policies)}


@router.post("/traffic/policies", dependencies=[Depends(_require_admin)])
async def create_traffic_policy(req: TrafficPolicyIn):
    if not req.pool_id.strip():
        raise HTTPException(status_code=400, detail="pool_id is required")
    policy_id = await asyncio.to_thread(
        _db.insert_traffic_policy,
        req.agent_role, req.system_id, req.pool_id.strip(), req.sticky,
    )
    await _store._refresh_traffic()
    return {"policy_id": policy_id, "status": "created"}


@router.delete("/traffic/policies/{policy_id}", dependencies=[Depends(_require_admin)])
async def delete_traffic_policy(policy_id: str):
    await asyncio.to_thread(_db.disable_traffic_policy, policy_id)
    await _store._refresh_traffic()
    return {"policy_id": policy_id, "status": "disabled"}


@router.get("/traffic/stats")
async def traffic_stats(hours: int = 1):
    """Per-model call stats for pool-routed traffic over the last N hours."""
    try:
        rows = await asyncio.to_thread(
            _db.fetch_all,
            f"SELECT model_used, routing_reason, "
            f"count() AS calls, "
            f"round(avg(latency_ms), 0) AS avg_latency_ms, "
            f"round(countIf(status != 'ok') / count(), 4) AS error_rate, "
            f"sum(tokens_in + tokens_out) AS total_tokens "
            f"FROM otel.gateway_call_log "
            f"WHERE created_at >= now() - INTERVAL {int(hours)} HOUR "
            f"AND routing_reason LIKE 'pool:%%' "
            f"GROUP BY model_used, routing_reason "
            f"ORDER BY calls DESC",
        )
    except Exception as exc:
        log.error("traffic_stats query failed: %s", exc)
        rows = []
    return {"stats": rows, "hours": hours, "endpoint_count": len(_store.pool_endpoints)}
