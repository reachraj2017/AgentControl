"""ProxyHandler — intercept, enforce, modify, forward via LiteLLM, log.

Features:
  • Routing policies with fallback model on upstream failure
  • Per-key rate limiting (requests/minute sliding window)
  • Response caching (exact-match, configurable TTL)
  • Streaming support (SSE, stream=true)
  • Provider key load-balancing (OPENAI_API_KEYS / ANTHROPIC_API_KEYS / GEMINI_API_KEYS)
  • Budget / cost alerts via webhook

Supported providers (via LiteLLM model prefix):
  openai/gpt-4o-mini                   → OpenAI
  anthropic/claude-haiku-4-5-20251001  → Anthropic
  ollama/llama3.2                      → Ollama (local)
  gemini/gemini-1.5-pro                → Google Gemini
  bedrock/anthropic.claude-v2          → AWS Bedrock
  <model-name> (no prefix)             → OpenAI (backward compatible)
"""

import asyncio
import hashlib
import itertools
import json
import logging
import os
import random
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from typing import AsyncIterator, Optional

import numpy as np

import httpx
import litellm
from fastapi.responses import JSONResponse, StreamingResponse

from enforcement import gate_check, phase2_enabled, wait_for_hitl
from telemetry import emit_call_span

log = logging.getLogger("gateway.proxy")

# LiteLLM global config
litellm.drop_params       = True
litellm.suppress_debug_info = True

_OPENAI_BASE_URL  = os.getenv("OPENAI_BASE_URL",  "https://api.openai.com/v1")
_OLLAMA_BASE_URL  = os.getenv("OLLAMA_BASE_URL",  "http://host.docker.internal:11434/v1")
_FORWARD_TIMEOUT  = float(os.getenv("GATEWAY_FORWARD_TIMEOUT_SECONDS", "60"))

_AUTH_ENABLED     = os.getenv("GATEWAY_AUTH_ENABLED", "false").lower() == "true"
_MASTER_KEY       = os.getenv("GATEWAY_MASTER_KEY", "")
_KEY_CACHE_TTL    = 60.0

# Response cache — set GATEWAY_CACHE_TTL_SECONDS > 0 to enable
_CACHE_TTL        = float(os.getenv("GATEWAY_CACHE_TTL_SECONDS", "0"))
_cache:     dict[str, tuple[dict, float]] = {}   # hash → (response_dict, expires_at)

# Semantic cache — embedding-based similarity matching
_SEMANTIC_TTL       = float(os.getenv("GATEWAY_SEMANTIC_CACHE_TTL_SECONDS", "0"))
_SEMANTIC_THRESHOLD = float(os.getenv("GATEWAY_SEMANTIC_CACHE_THRESHOLD", "0.92"))
_SEMANTIC_MAX       = int(os.getenv("GATEWAY_SEMANTIC_CACHE_MAX_ENTRIES", "500"))
_EMBED_MODEL        = os.getenv("GATEWAY_EMBED_MODEL", "text-embedding-3-small")
_sem_cache: list[dict] = []   # [{embedding, response, query, model, expires_at, ...}]

# Cost alert
_COST_ALERT_WEBHOOK   = os.getenv("GATEWAY_COST_ALERT_WEBHOOK",   "")
_COST_ALERT_DAILY_USD = float(os.getenv("GATEWAY_COST_ALERT_DAILY_USD", "0"))

# Simple per-model token cost estimate (USD per token, blended in/out)
_MODEL_COST_PER_TOKEN: dict[str, float] = {
    "gpt-4o-mini":       0.00000015,
    "gpt-4o":            0.0000025,
    "gpt-4.1-mini":      0.00000015,
    "gpt-4.1":           0.000002,
    "claude-haiku":      0.00000025,
    "claude-sonnet":     0.000003,
    "claude-opus":       0.000015,
    "gemini-1.5-flash":  0.000000075,
    "gemini-1.5-pro":    0.00000125,
    "llama":             0.0,   # local — no cost
}


def _estimate_cost_usd(model: str, tokens: int) -> float:
    m = model.lower()
    for key, rate in _MODEL_COST_PER_TOKEN.items():
        if key in m:
            return tokens * rate
    return tokens * 0.000001   # default fallback


# ── Provider key pools (load-balancing) ───────────────────────────────────────

def _build_key_pools() -> dict[str, itertools.cycle]:
    pools: dict[str, itertools.cycle] = {}
    for env_var, provider in [
        ("OPENAI_API_KEYS",    "openai"),
        ("ANTHROPIC_API_KEYS", "anthropic"),
        ("GEMINI_API_KEYS",    "gemini"),
    ]:
        raw = os.getenv(env_var, "").strip()
        if raw:
            keys = [k.strip() for k in raw.split(",") if k.strip()]
            if keys:
                pools[provider] = itertools.cycle(keys)
                log.info("key pool: %s has %d key(s)", provider, len(keys))
    return pools

_PROVIDER_KEY_POOLS = _build_key_pools()


def _get_provider_api_key(provider: str) -> Optional[str]:
    pool = _PROVIDER_KEY_POOLS.get(provider.lower())
    return next(pool) if pool else None


# ── Provider resolution ───────────────────────────────────────────────────────

def _resolve_litellm_model(target_model: str, target_backend: str) -> tuple[str, dict]:
    """Map (target_model, target_backend) → (litellm_model_string, extra_kwargs)."""
    extra: dict = {}

    if "/" in target_model:
        provider = target_model.split("/")[0].lower()
        if provider == "ollama":
            extra["api_base"] = _OLLAMA_BASE_URL.rstrip("/v1").rstrip("/")
        elif provider == "openai" and _OPENAI_BASE_URL != "https://api.openai.com/v1":
            extra["api_base"] = _OPENAI_BASE_URL
        # Inject pooled key for this provider
        pooled = _get_provider_api_key(provider)
        if pooled:
            extra["api_key"] = pooled
        return target_model, extra

    # No prefix — use target_backend for backward compatibility
    backend = (target_backend or "openai").lower()
    if backend == "ollama":
        extra["api_base"] = _OLLAMA_BASE_URL.rstrip("/v1").rstrip("/")
        return f"ollama/{target_model}", extra

    if _OPENAI_BASE_URL != "https://api.openai.com/v1":
        extra["api_base"] = _OPENAI_BASE_URL

    pooled = _get_provider_api_key(backend)
    if pooled:
        extra["api_key"] = pooled
    return target_model, extra


# ── Response cache helpers ────────────────────────────────────────────────────

def _cache_key(model: str, messages: list) -> str:
    raw = json.dumps({"model": model, "messages": messages}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def cache_get(key: str) -> Optional[dict]:
    entry = _cache.get(key)
    if entry and entry[1] > time.monotonic():
        return entry[0]
    if entry:
        del _cache[key]
    return None


def cache_put(key: str, response: dict, query_text: str = "") -> None:
    if _CACHE_TTL > 0:
        _cache[key] = (response, time.monotonic() + _CACHE_TTL, query_text)


def cache_flush() -> int:
    n = len(_cache)
    _cache.clear()
    return n


def cache_stats() -> dict:
    now = time.monotonic()
    live = sum(1 for v in _cache.values() if v[1] > now)
    return {"total_entries": len(_cache), "live_entries": live, "ttl_seconds": _CACHE_TTL}


def cache_entries() -> list:
    now = time.monotonic()
    entries = []
    for key_hash, entry in list(_cache.items()):
        response, expires_at, query_text = entry[0], entry[1], entry[2] if len(entry) > 2 else ""
        ttl_remaining = expires_at - now
        if ttl_remaining <= 0:
            continue
        model = response.get("model", "")
        choices = response.get("choices", [])
        response_preview = ""
        if choices:
            msg = (choices[0].get("message") or {}).get("content", "") or ""
            response_preview = msg[:120]
        usage = response.get("usage") or {}
        entries.append({
            "type":             "exact",
            "model":            model,
            "query":            query_text[:200],
            "ttl_remaining_s":  round(ttl_remaining),
            "prompt_tokens":    usage.get("prompt_tokens",     0),
            "completion_tokens":usage.get("completion_tokens", 0),
            "response_preview": response_preview,
        })
    entries.sort(key=lambda e: e["ttl_remaining_s"], reverse=True)
    return entries


# ── Semantic cache helpers ────────────────────────────────────────────────────

def _cosine_similarity(a: list[float], b: list[float]) -> float:
    va, vb = np.array(a, dtype=np.float32), np.array(b, dtype=np.float32)
    denom = np.linalg.norm(va) * np.linalg.norm(vb)
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


async def _get_embedding(text: str) -> list[float] | None:
    try:
        resp = await asyncio.wait_for(
            litellm.aembedding(model=_EMBED_MODEL, input=[text]),
            timeout=3.0,
        )
        return resp.data[0]["embedding"]
    except asyncio.TimeoutError:
        log.warning("semantic cache: embedding timed out (>3s), skipping")
        return None
    except Exception as exc:
        log.warning("semantic cache: embedding failed: %s", exc)
        return None


def _extract_query_text(messages: list) -> str:
    """Return the last user message content for embedding."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                parts = [p.get("text", "") for p in content if p.get("type") == "text"]
                return " ".join(parts)
            return str(content)
    return ""


async def semantic_cache_get(model: str, messages: list) -> tuple[dict | None, float]:
    """Return (cached_response, similarity_score) or (None, 0.0)."""
    if _SEMANTIC_TTL <= 0 or not _sem_cache:
        return None, 0.0
    query_text = _extract_query_text(messages)
    if not query_text:
        return None, 0.0
    embedding = await _get_embedding(query_text)
    if embedding is None:
        return None, 0.0
    now = time.monotonic()
    best_score, best_entry = 0.0, None
    for entry in _sem_cache:
        if entry["expires_at"] <= now:
            continue
        if entry["model"] != model:
            continue
        score = _cosine_similarity(embedding, entry["embedding"])
        if score > best_score:
            best_score, best_entry = score, entry
    if best_entry and best_score >= _SEMANTIC_THRESHOLD:
        return best_entry["response"], best_score
    return None, 0.0


async def semantic_cache_put(model: str, messages: list, response: dict) -> None:
    if _SEMANTIC_TTL <= 0:
        return
    query_text = _extract_query_text(messages)
    if not query_text:
        return
    embedding = await _get_embedding(query_text)
    if embedding is None:
        return
    choices = response.get("choices", [])
    preview = ""
    if choices:
        preview = ((choices[0].get("message") or {}).get("content", "") or "")[:120]
    usage = response.get("usage") or {}
    _sem_cache.append({
        "model":      model,
        "query":      query_text[:200],
        "embedding":  embedding,
        "response":   response,
        "expires_at": time.monotonic() + _SEMANTIC_TTL,
        "prompt_tokens":     usage.get("prompt_tokens", 0),
        "completion_tokens": usage.get("completion_tokens", 0),
        "response_preview":  preview,
    })
    # Prune expired entries and cap size
    now = time.monotonic()
    _sem_cache[:] = [e for e in _sem_cache if e["expires_at"] > now]
    if len(_sem_cache) > _SEMANTIC_MAX:
        _sem_cache[:] = _sem_cache[-_SEMANTIC_MAX:]


def semantic_cache_flush() -> int:
    n = len(_sem_cache)
    _sem_cache.clear()
    return n


def semantic_cache_stats() -> dict:
    now = time.monotonic()
    live = sum(1 for e in _sem_cache if e["expires_at"] > now)
    return {
        "total_entries":  len(_sem_cache),
        "live_entries":   live,
        "ttl_seconds":    _SEMANTIC_TTL,
        "threshold":      _SEMANTIC_THRESHOLD,
        "embed_model":    _EMBED_MODEL,
    }


def semantic_cache_entries() -> list:
    now = time.monotonic()
    result = []
    for e in _sem_cache:
        ttl_remaining = e["expires_at"] - now
        if ttl_remaining <= 0:
            continue
        result.append({
            "type":              "semantic",
            "model":             e["model"],
            "query":             e["query"],
            "ttl_remaining_s":   round(ttl_remaining),
            "prompt_tokens":     e["prompt_tokens"],
            "completion_tokens": e["completion_tokens"],
            "response_preview":  e["response_preview"],
        })
    result.sort(key=lambda e: e["ttl_remaining_s"], reverse=True)
    return result


# ── LiteLLM forwarding ────────────────────────────────────────────────────────

async def _forward(model: str, body: dict, extra: dict) -> dict:
    kwargs = {k: v for k, v in body.items() if k not in ("model", "stream")}
    resp = await litellm.acompletion(
        model=model,
        timeout=_FORWARD_TIMEOUT,
        **kwargs,
        **extra,
    )
    return json.loads(resp.model_dump_json())


async def _forward_stream(model: str, body: dict, extra: dict) -> AsyncIterator:
    kwargs = {k: v for k, v in body.items() if k not in ("model", "stream")}
    return await litellm.acompletion(
        model=model,
        timeout=_FORWARD_TIMEOUT,
        stream=True,
        stream_options={"include_usage": True},
        **kwargs,
        **extra,
    )


# ── Prompt helpers ────────────────────────────────────────────────────────────

def _last_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, str):
                return content[:2000]
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        return part.get("text", "")[:2000]
    return ""


# ── GatewayCallRecord enrichment helpers ─────────────────────────────────────
# messages_json / response_tool_calls_json feed M1's GatewayIngestPipeline
# target synthesis (tool_selection_accuracy, tool_argument_accuracy,
# tool_error_rate) without needing in-process spans.

def _messages_json(messages: list[dict]) -> str:
    try:
        return json.dumps(messages, ensure_ascii=False)[:32000]
    except Exception:
        return ""


def _response_tool_calls_json(llm_resp: Optional[dict]) -> str:
    if not llm_resp:
        return ""
    try:
        choices = llm_resp.get("choices", [])
        if not choices:
            return ""
        tool_calls = (choices[0].get("message") or {}).get("tool_calls") or []
        if not tool_calls:
            return ""
        return json.dumps(tool_calls, ensure_ascii=False)[:8000]
    except Exception:
        return ""


def _apply_mods(messages: list[dict], mods: list[dict]) -> tuple[list[dict], list[str]]:
    msgs    = list(messages)
    applied: list[str] = []

    for mod in mods:
        mod_type = mod.get("mod_type", "")
        content  = mod.get("content", "")
        mid      = str(mod.get("mod_id", ""))[:8]
        if not content:
            continue

        if mod_type == "system_prefix":
            if msgs and msgs[0].get("role") == "system":
                msgs[0] = {**msgs[0], "content": content + "\n\n" + msgs[0]["content"]}
            else:
                msgs.insert(0, {"role": "system", "content": content})
            applied.append(f"system_prefix:{mid}")

        elif mod_type == "system_suffix":
            if msgs and msgs[0].get("role") == "system":
                msgs[0] = {**msgs[0], "content": msgs[0]["content"] + "\n\n" + content}
            else:
                msgs.insert(0, {"role": "system", "content": content})
            applied.append(f"system_suffix:{mid}")

        elif mod_type == "few_shot":
            try:
                shots = json.loads(content)
                if isinstance(shots, list):
                    insert_at = len(msgs) - 1
                    for i in range(len(msgs) - 1, -1, -1):
                        if msgs[i].get("role") == "user":
                            insert_at = i
                            break
                    for shot in reversed(shots):
                        msgs.insert(insert_at, shot)
                    applied.append(f"few_shot:{mid}")
            except Exception:
                pass

    return msgs, applied


# ── Proxy handler ─────────────────────────────────────────────────────────────

class ProxyHandler:
    def __init__(self, change_store, db) -> None:
        self._store          = change_store
        self._db             = db
        self._key_cache:     dict[str, dict]  = {}
        self._key_cache_exp: dict[str, float] = {}
        # RPM sliding windows: key_id → deque of request timestamps
        self._rpm_windows:   dict[str, deque] = {}

    # ── Auth helpers ──────────────────────────────────────────────────────────

    def _validate_key_cached(self, raw_key: str) -> Optional[dict]:
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        now = time.monotonic()
        if key_hash in self._key_cache and self._key_cache_exp.get(key_hash, 0) > now:
            return self._key_cache[key_hash]
        record = self._db.validate_api_key(key_hash)
        if record:
            self._key_cache[key_hash]     = record
            self._key_cache_exp[key_hash] = now + _KEY_CACHE_TTL
        elif key_hash in self._key_cache:
            del self._key_cache[key_hash]
            del self._key_cache_exp[key_hash]
        return record

    def _check_rpm(self, key_id: str, limit: int) -> bool:
        """Return True if request is within rate limit, False if exceeded."""
        if limit <= 0:
            return True
        now = time.monotonic()
        window = self._rpm_windows.setdefault(key_id, deque())
        cutoff = now - 60.0
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= limit:
            return False
        window.append(now)
        return True

    # ── Budget alert ──────────────────────────────────────────────────────────

    def _fire_alert_async(self, webhook_url: str, payload: dict) -> None:
        asyncio.create_task(self._post_webhook(webhook_url, payload))

    async def _post_webhook(self, url: str, payload: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(url, json=payload)
        except Exception as e:
            log.debug("webhook post failed: %s", e)

    async def _check_budget_alerts(
        self, key_id: str, key_record: Optional[dict],
        model: str, tokens_in: int, tokens_out: int,
    ) -> None:
        total_tokens = tokens_in + tokens_out
        cost         = _estimate_cost_usd(model, total_tokens)

        # Global daily USD alert
        if _COST_ALERT_WEBHOOK and _COST_ALERT_DAILY_USD > 0:
            try:
                daily = await asyncio.to_thread(self._db.get_daily_cost_usd)
                if daily + cost >= _COST_ALERT_DAILY_USD:
                    self._fire_alert_async(_COST_ALERT_WEBHOOK, {
                        "alert": "daily_cost_threshold",
                        "daily_cost_usd": round(daily + cost, 4),
                        "threshold_usd":  _COST_ALERT_DAILY_USD,
                    })
            except Exception as e:
                log.debug("global budget check failed: %s", e)

        # Per-key USD alert
        if key_record and key_record.get("alert_webhook_url") and key_record.get("budget_alert_usd", 0) > 0:
            try:
                key_tokens = await asyncio.to_thread(self._db.get_key_daily_tokens, key_id)
                key_cost   = _estimate_cost_usd(model, key_tokens + total_tokens)
                if key_cost >= float(key_record["budget_alert_usd"]):
                    self._fire_alert_async(key_record["alert_webhook_url"], {
                        "alert":         "key_budget_threshold",
                        "key_prefix":    key_record.get("key_prefix", ""),
                        "cost_usd":      round(key_cost, 4),
                        "threshold_usd": float(key_record["budget_alert_usd"]),
                    })
            except Exception as e:
                log.debug("key budget check failed: %s", e)

    # ── Main handle ───────────────────────────────────────────────────────────

    async def handle(
        self,
        body:            dict,
        system_id:       str,
        agent_role:      str,
        run_id:           str,
        trace_id:        str,
        headers:         dict,
        conversation_id: str = "",
        protocol:        str = "openai.chat",
    ) -> JSONResponse | StreamingResponse:

        call_id         = str(uuid.uuid4())
        t_start         = time.monotonic()
        t_start_wall    = datetime.now(timezone.utc)
        model_requested = body.get("model", "gpt-4o-mini")
        messages        = list(body.get("messages", []))
        prompt_text     = _last_user_text(messages)
        want_stream     = body.get("stream") is True

        enforcement_result = "pass"
        mods_applied:  list[str] = []
        response_text  = ""
        tokens_in      = 0
        tokens_out     = 0
        ab_test_id     = ""
        ab_variant     = ""
        ab_test        = None
        key_id         = ""
        key_record:    Optional[dict] = None
        cache_hit      = False
        fallback_used  = False

        # ── 0. API key authentication ─────────────────────────────────────────
        def _log_reject(event_type, status, detail, kid="", kprefix=""):
            self._db.log_key_event(
                event_type=event_type, http_status=status, detail=detail,
                key_id=kid, key_prefix=kprefix,
                agent_role=agent_role, system_id=system_id,
                model_requested=model_requested,
            )

        if _AUTH_ENABLED:
            raw_key = headers.get("authorization", "").removeprefix("Bearer ").strip()
            if _MASTER_KEY and raw_key == _MASTER_KEY:
                key_id = "master"
            elif raw_key:
                kprefix = raw_key[:12]
                key_record = self._validate_key_cached(raw_key)
                if not key_record:
                    _log_reject("invalid_key", 401, "Invalid API key", kprefix=kprefix)
                    return JSONResponse(status_code=401, content={"error": {
                        "message": "Invalid API key",
                        "type": "authentication_error", "code": "invalid_api_key",
                    }})
                if not key_record.get("enabled"):
                    _log_reject("key_revoked", 403, "Key has been revoked",
                                kid=key_record["key_id"], kprefix=key_record.get("key_prefix",""))
                    return JSONResponse(status_code=403, content={"error": {
                        "message": "API key has been revoked",
                        "type": "authentication_error", "code": "key_revoked",
                    }})
                key_id = key_record["key_id"]
                kprefix = key_record.get("key_prefix", kprefix)

                # Role binding
                bound_role = key_record.get("agent_role", "*")
                if bound_role not in ("*", agent_role):
                    msg = f"Key bound to role '{bound_role}', got '{agent_role}'"
                    _log_reject("role_mismatch", 403, msg, kid=key_id, kprefix=kprefix)
                    return JSONResponse(status_code=403, content={"error": {
                        "message": f"Key not authorized for agent_role '{agent_role}'",
                        "type": "authorization_error", "code": "role_mismatch",
                    }})

                # Model allowlist — pre-routing check (fast path for direct requests)
                allowed_models = key_record.get("allowed_models") or []
                if allowed_models and model_requested not in allowed_models:
                    msg = f"Requested model '{model_requested}' not in allowlist {allowed_models}"
                    _log_reject("model_blocked", 403, msg, kid=key_id, kprefix=kprefix)
                    return JSONResponse(status_code=403, content={"error": {
                        "message": f"Model '{model_requested}' not in key's allowed_models",
                        "type": "authorization_error", "code": "model_not_allowed",
                    }})

                # Daily token budget
                daily_limit = int(key_record.get("daily_token_limit") or 0)
                if daily_limit > 0:
                    used = self._db.get_key_daily_tokens(key_id)
                    if used >= daily_limit:
                        msg = f"Used {used:,} of {daily_limit:,} daily tokens"
                        _log_reject("token_limit", 429, msg, kid=key_id, kprefix=kprefix)
                        return JSONResponse(status_code=429, content={"error": {
                            "message": f"Daily token limit ({daily_limit:,}) exceeded for this key",
                            "type": "budget_exceeded", "code": "token_limit_exceeded",
                        }})

                # Per-minute rate limit
                rpm_limit = int(key_record.get("rate_limit_rpm") or 0)
                if not self._check_rpm(key_id, rpm_limit):
                    msg = f"Exceeded {rpm_limit} req/min"
                    _log_reject("rate_limit", 429, msg, kid=key_id, kprefix=kprefix)
                    return JSONResponse(status_code=429, content={"error": {
                        "message": f"Rate limit exceeded ({rpm_limit} req/min for this key)",
                        "type": "rate_limit_exceeded", "code": "rate_limit_exceeded",
                    }})
            else:
                _log_reject("missing_key", 401, "No Authorization header")
                return JSONResponse(status_code=401, content={"error": {
                    "message": "Authentication required. Include 'Authorization: Bearer <gateway-key>'",
                    "type": "authentication_error", "code": "missing_api_key",
                }})

        # ── Pipeline shadow bypass ────────────────────────────────────────────
        is_pipeline_shadow = headers.get("x-gateway-shadow-pipeline", "").lower() == "true"

        # ── 1. Routing ────────────────────────────────────────────────────────
        if is_pipeline_shadow:
            target_model    = model_requested
            target_backend  = "openai"
            routing_reason  = "pipeline_shadow"
            fallback_model  = ""
            fallback_backend = "openai"
        else:
            # ── Traffic policy (pool-based endpoint selection) ────────────────
            traffic_policy   = self._store.get_traffic_policy(agent_role, system_id)
            traffic_endpoint = None
            if traffic_policy:
                traffic_endpoint = self._store.select_pool_endpoint(
                    traffic_policy, agent_role, conversation_id=trace_id
                )
            if traffic_endpoint:
                target_model     = traffic_endpoint["model"]
                target_backend   = traffic_endpoint.get("backend", "openai")
                routing_reason   = (
                    f"pool:{traffic_policy['pool_id'][:8]}:"
                    f"{traffic_endpoint.get('endpoint_id', '')[:8]}"
                )
                fallback_model   = ""
                fallback_backend = "openai"
            else:
                # ── A/B test ──────────────────────────────────────────────────
                ab_test = self._store.get_active_ab_test(agent_role, system_id)
                if ab_test:
                    ab_test_id = ab_test["test_id"]
                    if random.random() < float(ab_test.get("split_ratio", 0.5)):
                        ab_variant      = "B"
                        target_model    = ab_test.get("variant_b_model", "") or model_requested
                        target_backend  = ab_test.get("variant_b_backend", "openai")
                        routing_reason  = "ab_test_b"
                    else:
                        ab_variant      = "A"
                        target_model    = ab_test.get("variant_a_model", "") or model_requested
                        target_backend  = ab_test.get("variant_a_backend", "openai")
                        routing_reason  = "ab_test_a"
                    fallback_model   = ""
                    fallback_backend = "openai"
                else:
                    target_model, target_backend, routing_reason, fallback_model, fallback_backend = \
                        self._store.resolve_routing(agent_role, system_id, model_requested)

        litellm_model, extra_kw = _resolve_litellm_model(target_model, target_backend)

        # ── 1b. Post-routing model allowlist check ────────────────────────────
        # Routing policies, A/B tests, and shadow mode may resolve a different
        # model than what the agent requested. Always enforce the allowlist
        # against the FINAL resolved model — the originally-requested model
        # being allowed is not sufficient if the gateway changed it.
        if key_record:
            allowed_models = key_record.get("allowed_models") or []
            if allowed_models and target_model not in allowed_models:
                msg = (f"'{routing_reason}' resolved '{model_requested}' → '{target_model}', "
                       f"but '{target_model}' is not in allowlist {allowed_models}")
                _log_reject("model_blocked", 403, msg, kid=key_id,
                            kprefix=key_record.get("key_prefix", ""))
                return JSONResponse(status_code=403, content={"error": {
                    "message": (f"Gateway resolved model '{target_model}' "
                                f"via '{routing_reason}' which is not in this key's allowed_models"),
                    "type": "authorization_error", "code": "routed_model_not_allowed",
                }})

        # ── 2. Enforcement ────────────────────────────────────────────────────
        if not is_pipeline_shadow and phase2_enabled():
            decision, request_id = gate_check(
                agent_role, system_id, prompt_text, trace_id, run_id
            )

            if decision == "block":
                enforcement_result = "blocked"
                self._emit_async(dict(
                    call_id=call_id, trace_id=trace_id, run_id=run_id,
                    conversation_id=conversation_id, protocol=protocol,
                    started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
                    system_id=system_id, agent_role=agent_role,
                    model_requested=model_requested, model_used=litellm_model,
                    backend_used=target_backend, routing_reason=routing_reason,
                    mods_applied=[], prompt_text=prompt_text, response_text="[BLOCKED]",
                    tokens_in=0, tokens_out=0,
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                    enforcement_result="blocked", status="blocked",
                ))
                return JSONResponse(status_code=403, content={"error": {
                    "message": (
                        f"[GATEWAY BLOCK] {agent_role} blocked by governance "
                        "(circuit breaker OPEN)"
                    ),
                    "type": "governance_block",
                    "code": "circuit_breaker_open",
                }})

            if decision == "pause":
                outcome = await asyncio.to_thread(wait_for_hitl, request_id)
                if outcome != "approved":
                    enforcement_result = f"hitl_{outcome}"
                    self._emit_async(dict(
                        call_id=call_id, trace_id=trace_id, run_id=run_id,
                        conversation_id=conversation_id, protocol=protocol,
                        started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
                        system_id=system_id, agent_role=agent_role,
                        model_requested=model_requested, model_used=litellm_model,
                        backend_used=target_backend, routing_reason=routing_reason,
                        mods_applied=[], prompt_text=prompt_text,
                        response_text=f"[HITL {outcome.upper()}]",
                        tokens_in=0, tokens_out=0,
                        latency_ms=int((time.monotonic() - t_start) * 1000),
                        enforcement_result=enforcement_result, status="blocked",
                    ))
                    return JSONResponse(status_code=403, content={"error": {
                        "message": (
                            f"[GATEWAY BLOCK] {agent_role} requires HITL approval "
                            f"— {outcome}"
                        ),
                        "type": "hitl_required",
                        "code": f"hitl_{outcome}",
                    }})
                enforcement_result = "hitl_approved"

        # ── 3. Prompt modification injection ──────────────────────────────────
        if not is_pipeline_shadow:
            if ab_test and ab_variant:
                variant_prompt = ab_test.get(f"variant_{ab_variant.lower()}_prompt", "")
                if variant_prompt:
                    if messages and messages[0].get("role") == "system":
                        messages[0] = {**messages[0], "content": variant_prompt + "\n\n" + messages[0]["content"]}
                    else:
                        messages.insert(0, {"role": "system", "content": variant_prompt})
                    mods_applied.append(f"ab_{ab_variant.lower()}_prompt")
            mods = self._store.get_mods(agent_role, system_id)
            if mods:
                messages, standing_mods = _apply_mods(messages, mods)
                mods_applied.extend(standing_mods)

        # ── 4. Build forwarding body ──────────────────────────────────────────
        forward_body = {**body, "model": litellm_model, "messages": messages}
        forward_body.pop("stream", None)

        # ── 4b. Semantic cache check (skip mid-loop agentic calls) ───────────
        _has_tool_ctx = any(m.get("role") == "tool" for m in messages)
        if _SEMANTIC_TTL > 0 and not want_stream and not ab_test_id and not is_pipeline_shadow and not _has_tool_ctx:
            sem_resp, sem_score = await semantic_cache_get(litellm_model, messages)
            if sem_resp:
                sem_resp["gateway"] = {
                    "call_id":         call_id,
                    "model_requested": model_requested,
                    "model_used":      litellm_model,
                    "backend":         target_backend,
                    "routing_reason":  routing_reason,
                    "mods_applied":    mods_applied,
                    "enforcement":     enforcement_result,
                    "latency_ms":      0,
                    "cache_hit":       True,
                    "semantic_cache":  True,
                    "semantic_score":  round(sem_score, 4),
                    "ab_test_id":      ab_test_id,
                    "ab_variant":      ab_variant,
                }
                self._emit_async(dict(
                    call_id=call_id, trace_id=trace_id, run_id=run_id,
                    conversation_id=conversation_id, protocol=protocol,
                    started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
                    system_id=system_id, agent_role=agent_role,
                    model_requested=model_requested, model_used=litellm_model,
                    backend_used=target_backend, routing_reason=routing_reason,
                    mods_applied=mods_applied, prompt_text=prompt_text,
                    response_text=f"[SEMANTIC CACHE HIT score={sem_score:.4f}]",
                    tokens_in=0, tokens_out=0, latency_ms=0,
                    enforcement_result=enforcement_result, status="ok",
                    is_shadow=is_pipeline_shadow,
                    ab_test_id=ab_test_id, ab_variant=ab_variant,
                    key_id=key_id, cache_hit=1, fallback_used=0,
                ))
                return JSONResponse(content=sem_resp)

        # ── 5. Cache check (non-streaming only; skip A/B and shadow calls) ────
        ck: Optional[str] = None
        if _CACHE_TTL > 0 and not want_stream and not ab_test_id and not is_pipeline_shadow:
            ck      = _cache_key(litellm_model, messages)
            cached  = cache_get(ck)
            if cached:
                cache_hit = True
                cached["gateway"] = {
                    "call_id":         call_id,
                    "model_requested": model_requested,
                    "model_used":      litellm_model,
                    "backend":         target_backend,
                    "routing_reason":  routing_reason,
                    "mods_applied":    mods_applied,
                    "enforcement":     enforcement_result,
                    "latency_ms":      0,
                    "cache_hit":       True,
                    "ab_test_id":      ab_test_id,
                    "ab_variant":      ab_variant,
                }
                self._emit_async(dict(
                    call_id=call_id, trace_id=trace_id, run_id=run_id,
                    conversation_id=conversation_id, protocol=protocol,
                    started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
                    system_id=system_id, agent_role=agent_role,
                    model_requested=model_requested, model_used=litellm_model,
                    backend_used=target_backend, routing_reason=routing_reason,
                    mods_applied=mods_applied, prompt_text=prompt_text,
                    response_text="[CACHE HIT]",
                    tokens_in=0, tokens_out=0, latency_ms=0,
                    enforcement_result=enforcement_result, status="ok",
                    is_shadow=is_pipeline_shadow,
                    ab_test_id=ab_test_id, ab_variant=ab_variant,
                    key_id=key_id, cache_hit=1, fallback_used=0,
                ))
                return JSONResponse(content=cached)

        # ── 6. Streaming path ─────────────────────────────────────────────────
        if want_stream:
            base_record = dict(
                call_id=call_id, trace_id=trace_id, run_id=run_id,
                conversation_id=conversation_id, protocol=protocol,
                started_at=t_start_wall,
                system_id=system_id, agent_role=agent_role,
                model_requested=model_requested, model_used=litellm_model,
                backend_used=target_backend, routing_reason=routing_reason,
                mods_applied=mods_applied, prompt_text=prompt_text,
                messages_json=_messages_json(messages),
                enforcement_result=enforcement_result, is_shadow=is_pipeline_shadow,
                ab_test_id=ab_test_id, ab_variant=ab_variant,
                key_id=key_id, cache_hit=0, fallback_used=0,
            )
            gen = self._stream_and_log(
                litellm_model, forward_body, extra_kw,
                fallback_model, fallback_backend,
                t_start, base_record, key_record,
            )
            return StreamingResponse(gen, media_type="text/event-stream")

        # ── 7. Forward via LiteLLM (with fallback) ────────────────────────────
        llm_resp = None
        try:
            llm_resp   = await _forward(litellm_model, forward_body, extra_kw)
            usage      = llm_resp.get("usage") or {}
            tokens_in  = int(usage.get("prompt_tokens",     0) or 0)
            tokens_out = int(usage.get("completion_tokens", 0) or 0)
            choices    = llm_resp.get("choices", [])
            if choices:
                response_text = (
                    (choices[0].get("message") or {}).get("content", "") or ""
                )[:4000]
        except Exception as primary_err:
            log.warning("primary forward to %s failed: %s", litellm_model, primary_err)
            # Try fallback if configured
            if fallback_model:
                log.info("trying fallback model: %s", fallback_model)
                fb_litellm, fb_extra = _resolve_litellm_model(fallback_model, fallback_backend)
                try:
                    llm_resp   = await _forward(fb_litellm, forward_body, fb_extra)
                    usage      = llm_resp.get("usage") or {}
                    tokens_in  = int(usage.get("prompt_tokens",     0) or 0)
                    tokens_out = int(usage.get("completion_tokens", 0) or 0)
                    choices    = llm_resp.get("choices", [])
                    if choices:
                        response_text = (
                            (choices[0].get("message") or {}).get("content", "") or ""
                        )[:4000]
                    litellm_model  = fb_litellm
                    target_backend = fallback_backend
                    routing_reason = f"fallback_from_{routing_reason}"
                    fallback_used  = True
                except Exception as fallback_err:
                    log.warning("fallback to %s also failed: %s", fb_litellm, fallback_err)
                    llm_resp = None

            if llm_resp is None:
                latency_ms = int((time.monotonic() - t_start) * 1000)
                self._emit_async(dict(
                    call_id=call_id, trace_id=trace_id, run_id=run_id,
                    conversation_id=conversation_id, protocol=protocol,
                    started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
                    system_id=system_id, agent_role=agent_role,
                    model_requested=model_requested, model_used=litellm_model,
                    backend_used=target_backend, routing_reason=routing_reason,
                    mods_applied=mods_applied, prompt_text=prompt_text,
                    response_text=f"[ERROR] {primary_err}",
                    tokens_in=0, tokens_out=0, latency_ms=latency_ms,
                    enforcement_result=enforcement_result, status="error",
                    ab_test_id=ab_test_id, ab_variant=ab_variant,
                    key_id=key_id, cache_hit=0, fallback_used=0,
                ))
                return JSONResponse(status_code=502, content={"error": {
                    "message": str(primary_err),
                    "type":    "gateway_upstream_error",
                }})

        latency_ms = int((time.monotonic() - t_start) * 1000)

        # ── 8. Store in cache if eligible (skip tool-call responses) ─────────
        _resp_has_tool_calls = bool(
            (llm_resp or {}).get("choices", [{}])[0].get("message", {}).get("tool_calls")
        )
        _msgs_have_tool_results = any(m.get("role") == "tool" for m in messages)
        _cacheable = llm_resp and not _resp_has_tool_calls and not _msgs_have_tool_results
        if ck and _cacheable:
            cache_put(ck, dict(llm_resp), _extract_query_text(messages))
        if _SEMANTIC_TTL > 0 and _cacheable and not want_stream and not ab_test_id and not is_pipeline_shadow:
            asyncio.get_event_loop().create_task(semantic_cache_put(litellm_model, messages, dict(llm_resp)))

        # ── 9. Build call record ──────────────────────────────────────────────
        call_record = dict(
            call_id=call_id, trace_id=trace_id, run_id=run_id,
            conversation_id=conversation_id, protocol=protocol,
            started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
            messages_json=_messages_json(messages),
            response_tool_calls_json=_response_tool_calls_json(llm_resp),
            system_id=system_id, agent_role=agent_role,
            model_requested=model_requested, model_used=litellm_model,
            backend_used=target_backend, routing_reason=routing_reason,
            mods_applied=mods_applied, prompt_text=prompt_text,
            response_text=response_text,
            tokens_in=tokens_in, tokens_out=tokens_out,
            latency_ms=latency_ms,
            enforcement_result=enforcement_result, status="ok",
            is_shadow=is_pipeline_shadow,
            ab_test_id=ab_test_id, ab_variant=ab_variant,
            key_id=key_id,
            cache_hit=1 if cache_hit else 0,
            fallback_used=1 if fallback_used else 0,
        )

        # ── 10. Async: ClickHouse log + OTel span + budget checks ─────────────
        self._emit_async(call_record)
        if tokens_in + tokens_out > 0:
            asyncio.create_task(
                self._check_budget_alerts(key_id, key_record, litellm_model, tokens_in, tokens_out)
            )

        # ── 11. Async: call-level shadow ──────────────────────────────────────
        if not is_pipeline_shadow:
            shadow_rule = self._store.get_shadow_rule(agent_role, system_id)
            if shadow_rule:
                asyncio.create_task(
                    self._shadow_call(shadow_rule, forward_body, call_record)
                )

        # ── 12. Return response with gateway metadata ─────────────────────────
        llm_resp["gateway"] = {
            "call_id":         call_id,
            "model_requested": model_requested,
            "model_used":      litellm_model,
            "backend":         target_backend,
            "routing_reason":  routing_reason,
            "mods_applied":    mods_applied,
            "enforcement":     enforcement_result,
            "latency_ms":      latency_ms,
            "ab_test_id":      ab_test_id,
            "ab_variant":      ab_variant,
            "cache_hit":       cache_hit,
            "fallback_used":   fallback_used,
        }
        return JSONResponse(content=llm_resp)

    # ── Streaming helper ──────────────────────────────────────────────────────

    async def _stream_and_log(
        self,
        litellm_model:    str,
        forward_body:     dict,
        extra_kw:         dict,
        fallback_model:   str,
        fallback_backend: str,
        t_start:          float,
        base_record:      dict,
        key_record:       Optional[dict],
    ) -> AsyncIterator[bytes]:
        accumulated = ""
        tokens_in   = 0
        tokens_out  = 0
        used_model  = litellm_model

        async def _run_stream(model, body, extra):
            nonlocal accumulated, tokens_in, tokens_out, used_model
            stream = await _forward_stream(model, body, extra)
            async for chunk in stream:
                chunk_dict = json.loads(chunk.model_dump_json())
                choices = chunk_dict.get("choices", [])
                if choices:
                    delta = (choices[0].get("delta") or {}).get("content") or ""
                    accumulated += delta
                if chunk_dict.get("usage"):
                    u = chunk_dict["usage"]
                    tokens_in  = int(u.get("prompt_tokens",     0) or 0)
                    tokens_out = int(u.get("completion_tokens", 0) or 0)
                yield f"data: {json.dumps(chunk_dict)}\n\n".encode()
            yield b"data: [DONE]\n\n"

        try:
            async for chunk_bytes in _run_stream(litellm_model, forward_body, extra_kw):
                yield chunk_bytes
        except Exception as primary_err:
            log.warning("stream forward to %s failed: %s", litellm_model, primary_err)
            if fallback_model:
                fb_litellm, fb_extra = _resolve_litellm_model(fallback_model, fallback_backend)
                used_model = fb_litellm
                log.info("stream: trying fallback model %s", fb_litellm)
                try:
                    async for chunk_bytes in _run_stream(fb_litellm, forward_body, fb_extra):
                        yield chunk_bytes
                except Exception as fb_err:
                    err = {"error": {"message": str(fb_err), "type": "gateway_upstream_error"}}
                    yield f"data: {json.dumps(err)}\n\n".encode()
                    yield b"data: [DONE]\n\n"
            else:
                err = {"error": {"message": str(primary_err), "type": "gateway_upstream_error"}}
                yield f"data: {json.dumps(err)}\n\n".encode()
                yield b"data: [DONE]\n\n"
        finally:
            latency_ms = int((time.monotonic() - t_start) * 1000)
            record = {
                **base_record,
                "model_used":     used_model,
                "response_text":  accumulated[:4000],
                "tokens_in":      tokens_in,
                "tokens_out":     tokens_out,
                "latency_ms":     latency_ms,
                "status":         "ok",
                "fallback_used":  1 if used_model != litellm_model else 0,
                "ended_at":       datetime.now(timezone.utc),
            }
            self._emit_async(record)
            if tokens_in + tokens_out > 0:
                asyncio.create_task(
                    self._check_budget_alerts(
                        base_record.get("key_id", ""), key_record,
                        used_model, tokens_in, tokens_out,
                    )
                )

    # ── Async helpers ─────────────────────────────────────────────────────────

    def _emit_async(self, record: dict) -> None:
        asyncio.create_task(self._do_emit(record))

    async def _do_emit(self, record: dict) -> None:
        try:
            await asyncio.to_thread(self._db.log_call, record)
            await asyncio.to_thread(emit_call_span, record)
        except Exception as e:
            log.debug("async emit failed: %s", e)

    async def _shadow_call(
        self,
        rule:           dict,
        primary_body:   dict,
        primary_record: dict,
    ) -> None:
        try:
            shadow_litellm, shadow_extra = _resolve_litellm_model(
                rule["shadow_model"], rule.get("shadow_backend", "openai")
            )
            shadow_body = {**primary_body, "model": shadow_litellm}
            resp        = await _forward(shadow_litellm, shadow_body, shadow_extra)
            usage       = resp.get("usage") or {}
            choices     = resp.get("choices", [])
            shadow_text = ""
            if choices:
                shadow_text = (
                    (choices[0].get("message") or {}).get("content", "") or ""
                )[:4000]
            shadow_record = {
                **primary_record,
                "call_id":        str(uuid.uuid4()),
                "is_shadow":      True,
                "model_used":     shadow_litellm,
                "backend_used":   rule.get("shadow_backend", "openai"),
                "routing_reason": "shadow",
                "response_text":  shadow_text,
                "tokens_in":      int(usage.get("prompt_tokens",     0) or 0),
                "tokens_out":     int(usage.get("completion_tokens", 0) or 0),
                "cache_hit":      0,
                "fallback_used":  0,
            }
            await asyncio.to_thread(self._db.log_call, shadow_record)
            await asyncio.to_thread(emit_call_span, shadow_record)
            log.debug("shadow call done: model=%s", shadow_litellm)
        except Exception as e:
            log.debug("shadow call failed: %s", e)

    # ── v4: shared auth (used by /v1/embeddings and other lean endpoints) ────
    # Factored out of the top of handle() so protocol handlers that don't need
    # the full routing/enforcement/cache pipeline (e.g. embeddings) can still
    # reuse the same virtual-key auth, rather than re-implementing it.

    def authenticate(
        self, headers: dict, agent_role: str, model_requested: str = "",
    ) -> tuple[Optional[JSONResponse], str, Optional[dict]]:
        """Returns (error_response_or_None, key_id, key_record)."""
        if not _AUTH_ENABLED:
            return None, "", None

        def _log_reject(event_type, status, detail, kid="", kprefix=""):
            self._db.log_key_event(
                event_type=event_type, http_status=status, detail=detail,
                key_id=kid, key_prefix=kprefix, agent_role=agent_role,
                model_requested=model_requested,
            )

        raw_key = headers.get("authorization", "").removeprefix("Bearer ").strip()
        if _MASTER_KEY and raw_key == _MASTER_KEY:
            return None, "master", None
        if not raw_key:
            _log_reject("missing_key", 401, "No Authorization header")
            return JSONResponse(status_code=401, content={"error": {
                "message": "Authentication required. Include 'Authorization: Bearer <gateway-key>'",
                "type": "authentication_error", "code": "missing_api_key",
            }}), "", None

        kprefix = raw_key[:12]
        key_record = self._validate_key_cached(raw_key)
        if not key_record:
            _log_reject("invalid_key", 401, "Invalid API key", kprefix=kprefix)
            return JSONResponse(status_code=401, content={"error": {
                "message": "Invalid API key",
                "type": "authentication_error", "code": "invalid_api_key",
            }}), "", None
        if not key_record.get("enabled"):
            _log_reject("key_revoked", 403, "Key has been revoked",
                        kid=key_record["key_id"], kprefix=key_record.get("key_prefix", ""))
            return JSONResponse(status_code=403, content={"error": {
                "message": "API key has been revoked",
                "type": "authentication_error", "code": "key_revoked",
            }}), "", None
        return None, key_record["key_id"], key_record

    # ── v4: /v1/embeddings — lean endpoint, not a chat call ──────────────────
    # Reuses the same virtual-key auth as chat/responses/messages, but does not
    # go through routing/governance/cache — embeddings carry no prompt-injection
    # or generation-quality surface the way a completion does. Logged with
    # protocol="embedding" so it's still visible in the same call log.

    async def handle_embeddings(
        self, body: dict, system_id: str, agent_role: str,
        run_id: str, trace_id: str, headers: dict,
    ) -> JSONResponse:
        t_start = time.monotonic()
        t_start_wall = datetime.now(timezone.utc)
        call_id = str(uuid.uuid4())
        model_requested = body.get("model", _EMBED_MODEL)

        err, key_id, _ = self.authenticate(headers, agent_role, model_requested)
        if err is not None:
            return err

        try:
            resp = await litellm.aembedding(model=model_requested, input=body.get("input", []))
            resp_dict = json.loads(resp.model_dump_json())
        except Exception as exc:
            self._emit_async(dict(
                call_id=call_id, trace_id=trace_id, run_id=run_id,
                conversation_id=headers.get("x-gateway-conversation-id", ""),
                protocol="embedding", started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
                system_id=system_id, agent_role=agent_role,
                model_requested=model_requested, model_used=model_requested,
                backend_used="openai", routing_reason="embedding",
                prompt_text="", response_text=f"[ERROR] {exc}",
                tokens_in=0, tokens_out=0,
                latency_ms=int((time.monotonic() - t_start) * 1000),
                status="error", key_id=key_id,
            ))
            return JSONResponse(status_code=502, content={"error": {
                "message": str(exc), "type": "gateway_upstream_error",
            }})

        usage = resp_dict.get("usage") or {}
        self._emit_async(dict(
            call_id=call_id, trace_id=trace_id, run_id=run_id,
            conversation_id=headers.get("x-gateway-conversation-id", ""),
            protocol="embedding", started_at=t_start_wall, ended_at=datetime.now(timezone.utc),
            system_id=system_id, agent_role=agent_role,
            model_requested=model_requested, model_used=model_requested,
            backend_used="openai", routing_reason="embedding",
            prompt_text="", response_text="",
            tokens_in=int(usage.get("prompt_tokens", 0) or 0), tokens_out=0,
            latency_ms=int((time.monotonic() - t_start) * 1000),
            status="ok", key_id=key_id,
        ))
        return JSONResponse(content=resp_dict)

    # ── Checkpoint / handoff / tool-span ──────────────────────────────────────
    # Same front door (auth, durable logging) as LLM traffic, for the signals
    # that never cross the LLM wire: pre-action gates, sub-agent handoffs, and
    # non-LLM tool executions. Fronts the SAME governance gate_check() the
    # inline enforcement path already uses (§2 of handle()) — no new decision
    # logic, just a shared entry point.

    async def handle_checkpoint(self, body: dict, headers: dict) -> JSONResponse:
        agent_role      = body.get("agent_role", "unknown")
        system_id       = body.get("system_id", "unknown")
        conversation_id = body.get("conversation_id", "")
        action          = body.get("action", "")
        risk_level      = body.get("risk_level", "medium")

        err, key_id, _ = self.authenticate(headers, agent_role)
        if err is not None:
            return err

        checkpoint_id = str(uuid.uuid4())
        decision = "allow"
        reason = ""

        if phase2_enabled():
            query = f"[checkpoint:{risk_level}] {action}"
            gate_decision, request_id = gate_check(
                agent_role, system_id, query, conversation_id, conversation_id,
            )
            if gate_decision == "block":
                decision, reason = "block", "blocked by governance policy"
            elif gate_decision == "pause":
                outcome = await asyncio.to_thread(wait_for_hitl, request_id)
                if outcome == "approved":
                    decision, reason = "allow", "hitl_approved"
                else:
                    decision, reason = "block", f"hitl_{outcome}"

        await asyncio.to_thread(
            self._db.log_structural_event,
            call_type="checkpoint",
            conversation_id=conversation_id,
            run_id=body.get("run_id", ""),
            system_id=system_id,
            agent_role=agent_role,
            payload={**body, "checkpoint_id": checkpoint_id, "decision": decision, "reason": reason},
        )
        return JSONResponse(content={
            "decision": decision, "checkpoint_id": checkpoint_id, "reason": reason,
        })

    async def handle_handoff(self, body: dict, headers: dict) -> JSONResponse:
        agent_role = body.get("from_agent", "unknown")
        err, _, _ = self.authenticate(headers, agent_role)
        if err is not None:
            return err
        event_id = await asyncio.to_thread(
            self._db.log_structural_event,
            call_type="handoff",
            conversation_id=body.get("conversation_id", ""),
            run_id=body.get("run_id", ""),
            system_id=body.get("system_id", ""),
            agent_role=agent_role,
            payload=body,
        )
        return JSONResponse(content={"event_id": event_id, "status": "recorded"})

    async def handle_tool_span(self, body: dict, headers: dict) -> JSONResponse:
        agent_role = body.get("agent_role", "unknown")
        err, _, _ = self.authenticate(headers, agent_role)
        if err is not None:
            return err
        event_id = await asyncio.to_thread(
            self._db.log_structural_event,
            call_type="tool_span",
            conversation_id=body.get("conversation_id", ""),
            run_id=body.get("run_id", ""),
            system_id=body.get("system_id", ""),
            agent_role=agent_role,
            payload=body,
        )
        return JSONResponse(content={"event_id": event_id, "status": "recorded"})
