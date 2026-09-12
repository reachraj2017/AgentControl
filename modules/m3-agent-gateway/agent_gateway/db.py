"""GatewayDB — ClickHouse client for the agent gateway.

Creates and manages ten gateway-specific tables:
  gateway_routing_policies  — per-role/system model routing overrides
  gateway_prompt_mods       — approved prompt modifications (prefix/suffix/few-shot)
  gateway_shadow_rules      — shadow mode configuration
  gateway_call_log          — immutable log of every LLM call
  gateway_proposed_changes  — reflection-agent proposals awaiting operator approval
  gateway_shadow_evals      — LLM judge scores for shadow calls
  gateway_ab_tests          — A/B test configurations and lifecycle state
  gateway_endpoint_pools    — named pools of LLM endpoints with a selection strategy
  gateway_pool_endpoints    — individual endpoints within a pool
  gateway_traffic_policies  — maps agent_role/system_id to an endpoint pool
"""

import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from clickhouse_driver import Client

log = logging.getLogger("gateway.db")

_DDL = [
    """CREATE TABLE IF NOT EXISTS otel.gateway_routing_policies (
        policy_id      String  DEFAULT generateUUIDv4(),
        agent_role     String  DEFAULT '*',
        system_id      String  DEFAULT '*',
        model_match    String  DEFAULT '',
        target_model   String,
        target_backend LowCardinality(String) DEFAULT 'openai',
        reason         String  DEFAULT '',
        enabled        UInt8   DEFAULT 1,
        approved_by    String  DEFAULT 'system',
        updated_at     DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (agent_role, system_id, policy_id)""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_prompt_mods (
        mod_id         String  DEFAULT generateUUIDv4(),
        agent_role     String  DEFAULT '*',
        system_id      String  DEFAULT '*',
        mod_type       LowCardinality(String) DEFAULT 'system_prefix',
        content        String,
        evidence_delta Float32 DEFAULT 0.0,
        enabled        UInt8   DEFAULT 1,
        approved_by    String  DEFAULT 'system',
        updated_at     DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (agent_role, mod_id)""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_shadow_rules (
        rule_id        String  DEFAULT generateUUIDv4(),
        agent_role     String  DEFAULT '*',
        system_id      String  DEFAULT '*',
        shadow_model   String,
        shadow_backend LowCardinality(String) DEFAULT 'openai',
        sample_rate    Float32 DEFAULT 0.1,
        enabled        UInt8   DEFAULT 1,
        updated_at     DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (agent_role, rule_id)""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_call_log (
        call_id            String DEFAULT generateUUIDv4(),
        trace_id           String DEFAULT '',
        session_id         String DEFAULT '',
        system_id          LowCardinality(String) DEFAULT '',
        agent_role         LowCardinality(String) DEFAULT '',
        model_requested    String DEFAULT '',
        model_used         String DEFAULT '',
        backend_used       LowCardinality(String) DEFAULT 'openai',
        routing_reason     String DEFAULT '',
        mods_applied       Array(String) DEFAULT [],
        prompt_text        String DEFAULT '',
        response_text      String DEFAULT '',
        tokens_in          UInt32 DEFAULT 0,
        tokens_out         UInt32 DEFAULT 0,
        latency_ms         UInt32 DEFAULT 0,
        is_shadow          UInt8  DEFAULT 0,
        enforcement_result LowCardinality(String) DEFAULT 'pass',
        status             LowCardinality(String) DEFAULT 'ok',
        run_id             String DEFAULT '',
        conversation_id    String DEFAULT '',
        protocol           LowCardinality(String) DEFAULT 'openai.chat',
        started_at         DateTime64(3) DEFAULT now64(3),
        ended_at           DateTime64(3) DEFAULT now64(3),
        messages_json      String DEFAULT '',
        response_tool_calls_json String DEFAULT '',
        created_at         DateTime64(3) DEFAULT now64(3)
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(created_at)
    ORDER BY (system_id, agent_role, created_at)
    SETTINGS index_granularity = 8192""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_proposed_changes (
        change_id    String  DEFAULT generateUUIDv4(),
        change_type  LowCardinality(String),
        agent_role   String  DEFAULT '*',
        system_id    String  DEFAULT '*',
        description  String  DEFAULT '',
        payload      String  DEFAULT '',
        evidence     String  DEFAULT '',
        status       LowCardinality(String) DEFAULT 'pending',
        proposed_by  String  DEFAULT 'reflection-agent',
        approved_by  String  DEFAULT '',
        proposed_at  DateTime DEFAULT now(),
        decided_at   DateTime DEFAULT '1970-01-01 00:00:00'
    ) ENGINE = ReplacingMergeTree(decided_at)
    ORDER BY change_id""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_pipeline_shadow_evals (
        eval_id              String        DEFAULT generateUUIDv4(),
        agent_role           String        DEFAULT '',
        user_input           String        DEFAULT '',
        primary_model        String        DEFAULT '',
        shadow_model         String        DEFAULT '',
        primary_response     String        DEFAULT '',
        shadow_response      String        DEFAULT '',
        primary_tokens       UInt32        DEFAULT 0,
        shadow_tokens        UInt32        DEFAULT 0,
        primary_latency_ms   UInt32        DEFAULT 0,
        shadow_latency_ms    UInt32        DEFAULT 0,
        primary_faithfulness Float32       DEFAULT 0,
        primary_relevance    Float32       DEFAULT 0,
        primary_instruction  Float32       DEFAULT 0,
        shadow_faithfulness  Float32       DEFAULT 0,
        shadow_relevance     Float32       DEFAULT 0,
        shadow_instruction   Float32       DEFAULT 0,
        created_at           DateTime64(3) DEFAULT now64(3)
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(created_at)
    ORDER BY (agent_role, created_at)""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_shadow_evals (
        shadow_eval_id  String              DEFAULT generateUUIDv4(),
        call_id         String              DEFAULT '',
        agent_role      String              DEFAULT '',
        model_used      String              DEFAULT '',
        prompt_text     String              DEFAULT '',
        response_text   String              DEFAULT '',
        scores          Map(String, Float32) DEFAULT map(),
        scored_at       DateTime64(3)       DEFAULT now64(3)
    ) ENGINE = MergeTree()
    PARTITION BY toYYYYMM(scored_at)
    ORDER BY (agent_role, scored_at)""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_api_keys (
        key_id            String                 DEFAULT generateUUIDv4(),
        key_hash          String                 DEFAULT '',
        key_prefix        String                 DEFAULT '',
        description       String                 DEFAULT '',
        agent_role        String                 DEFAULT '*',
        system_id         String                 DEFAULT '*',
        allowed_models    Array(String)          DEFAULT [],
        daily_token_limit UInt32                 DEFAULT 0,
        is_admin          UInt8                  DEFAULT 0,
        enabled           UInt8                  DEFAULT 1,
        created_at        DateTime               DEFAULT now(),
        updated_at        DateTime               DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY key_id""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_ab_tests (
        test_id           String                 DEFAULT generateUUIDv4(),
        test_name         String                 DEFAULT '',
        agent_role        String                 DEFAULT '*',
        system_id         String                 DEFAULT '*',
        variant_a_model   String                 DEFAULT '',
        variant_a_backend LowCardinality(String) DEFAULT 'openai',
        variant_a_prompt  String                 DEFAULT '',
        variant_b_model   String                 DEFAULT '',
        variant_b_backend LowCardinality(String) DEFAULT 'openai',
        variant_b_prompt  String                 DEFAULT '',
        split_ratio       Float32                DEFAULT 0.5,
        status            LowCardinality(String) DEFAULT 'draft',
        created_at        DateTime               DEFAULT now(),
        updated_at        DateTime               DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (agent_role, test_id)""",
    """CREATE TABLE IF NOT EXISTS otel.gateway_key_events (
        event_id        String         DEFAULT generateUUIDv4(),
        ts              DateTime64(3)  DEFAULT now64(3),
        key_id          String         DEFAULT '',
        key_prefix      String         DEFAULT '',
        agent_role      String         DEFAULT '',
        system_id       String         DEFAULT '',
        event_type      LowCardinality(String) DEFAULT '',
        http_status     UInt16         DEFAULT 0,
        model_requested String         DEFAULT '',
        detail          String         DEFAULT ''
    ) ENGINE = MergeTree
    PARTITION BY toYYYYMM(ts)
    ORDER BY (ts, key_id)
    SETTINGS index_granularity = 8192""",

    # ── Traffic management ────────────────────────────────────────────────────

    """CREATE TABLE IF NOT EXISTS otel.gateway_endpoint_pools (
        pool_id     String                 DEFAULT generateUUIDv4(),
        name        String                 DEFAULT '',
        strategy    LowCardinality(String) DEFAULT 'round_robin',
        description String                 DEFAULT '',
        enabled     UInt8                  DEFAULT 1,
        created_at  DateTime               DEFAULT now(),
        updated_at  DateTime               DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY pool_id""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_pool_endpoints (
        endpoint_id String                 DEFAULT generateUUIDv4(),
        pool_id     String                 DEFAULT '',
        model       String                 DEFAULT '',
        backend     LowCardinality(String) DEFAULT 'openai',
        weight      Float32                DEFAULT 1.0,
        priority    UInt8                  DEFAULT 1,
        enabled     UInt8                  DEFAULT 1,
        created_at  DateTime               DEFAULT now(),
        updated_at  DateTime               DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (pool_id, endpoint_id)""",

    """CREATE TABLE IF NOT EXISTS otel.gateway_traffic_policies (
        policy_id  String  DEFAULT generateUUIDv4(),
        agent_role String  DEFAULT '*',
        system_id  String  DEFAULT '*',
        pool_id    String  DEFAULT '',
        sticky     UInt8   DEFAULT 0,
        enabled    UInt8   DEFAULT 1,
        created_at DateTime DEFAULT now(),
        updated_at DateTime DEFAULT now()
    ) ENGINE = ReplacingMergeTree(updated_at)
    ORDER BY (agent_role, system_id, policy_id)""",

    # ── Gateway-primary ingest ────────────────────────────────────────────────
    # gateway_call_eval_state is written by M1 (eval-runner), not the gateway —
    # the gateway only needs the table to exist so M1's ingest pipeline can rely
    # on it from container start. Declared here defensively (IF NOT EXISTS);
    # M1 also declares it in its own DDL.
    """CREATE TABLE IF NOT EXISTS otel.gateway_call_eval_state (
        call_id     String,
        state       LowCardinality(String) DEFAULT 'pending',
        trace_id    String DEFAULT '',
        updated_at  DateTime64(3) DEFAULT now64(3)
    ) ENGINE = ReplacingMergeTree(updated_at) ORDER BY call_id""",

    # gateway_structural_events — checkpoint / handoff / tool_span records.
    # Written by the gateway (POST /v1/checkpoint, /v1/handoff, /v1/tool-span),
    # read by M1's GatewayIngestPipeline to synthesise handoff_spans/tool_spans.
    """CREATE TABLE IF NOT EXISTS otel.gateway_structural_events (
        event_id        String DEFAULT generateUUIDv4(),
        call_type       LowCardinality(String),
        conversation_id String DEFAULT '',
        run_id          String DEFAULT '',
        system_id       String DEFAULT '',
        agent_role      String DEFAULT '',
        payload_json    String,
        created_at      DateTime64(3) DEFAULT now64(3)
    ) ENGINE = MergeTree() ORDER BY (conversation_id, created_at)""",
]

# ALTER TABLE statements for columns added to existing tables
_ALTER_STMTS = [
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS ab_test_id String DEFAULT ''",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS ab_variant  String DEFAULT ''",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS key_id      String DEFAULT ''",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS cache_hit   UInt8  DEFAULT 0",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS fallback_used UInt8 DEFAULT 0",
    # Routing policies — fallback model support
    "ALTER TABLE otel.gateway_routing_policies ADD COLUMN IF NOT EXISTS fallback_model   String DEFAULT ''",
    "ALTER TABLE otel.gateway_routing_policies ADD COLUMN IF NOT EXISTS fallback_backend LowCardinality(String) DEFAULT 'openai'",
    # API keys — rate limiting + per-key budget alerts
    "ALTER TABLE otel.gateway_api_keys ADD COLUMN IF NOT EXISTS rate_limit_rpm    UInt32  DEFAULT 0",
    "ALTER TABLE otel.gateway_api_keys ADD COLUMN IF NOT EXISTS budget_alert_usd  Float32 DEFAULT 0",
    "ALTER TABLE otel.gateway_api_keys ADD COLUMN IF NOT EXISTS alert_webhook_url String  DEFAULT ''",
    # Traffic management — pool used on a call
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS pool_id String DEFAULT ''",
    # v4 — gateway-primary ingest (GatewayCallRecord contract, real call timing, protocol identity)
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS conversation_id String DEFAULT ''",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS protocol LowCardinality(String) DEFAULT 'openai.chat'",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS started_at DateTime64(3) DEFAULT now64(3)",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS ended_at DateTime64(3) DEFAULT now64(3)",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS messages_json String DEFAULT ''",
    "ALTER TABLE otel.gateway_call_log ADD COLUMN IF NOT EXISTS response_tool_calls_json String DEFAULT ''",
]


class GatewayDB:
    def __init__(self) -> None:
        self.host     = os.getenv("CLICKHOUSE_HOST",     "clickhouse")
        self.port     = int(os.getenv("CLICKHOUSE_PORT", "9000"))
        self.database = os.getenv("CLICKHOUSE_DB",       "otel")
        self.user     = os.getenv("CLICKHOUSE_USER",     "default")
        self.password = os.getenv("CLICKHOUSE_PASSWORD", "")
        self._client: Optional[Client] = None

    # ── Connection ────────────────────────────────────────────────────────────

    def _get_client(self) -> Client:
        if self._client is None:
            self._client = Client(
                host=self.host, port=self.port,
                database=self.database, user=self.user, password=self.password,
                settings={"connect_timeout": 10, "send_receive_timeout": 30},
            )
        return self._client

    def _reconnect(self) -> Client:
        self._client = None
        return self._get_client()

    # ── Helpers ───────────────────────────────────────────────────────────────

    def execute(self, query: str, params: Any = None) -> Any:
        try:
            return self._get_client().execute(query, params or [])
        except Exception as e:
            log.warning("db execute failed: %s", e)
            try:
                return self._reconnect().execute(query, params or [])
            except Exception as e2:
                log.error("db execute retry failed: %s", e2)
                raise

    def fetch_all(self, query: str, params: Any = None) -> list[dict]:
        try:
            rows, cols = self._get_client().execute(query, params or {}, with_column_types=True)
            names = [c[0] for c in cols]
            return [dict(zip(names, r)) for r in rows]
        except Exception as e:
            log.error("db fetch_all failed: %s", e)
            raise

    def fetch_one(self, query: str, params: Any = None) -> Optional[dict]:
        rows = self.fetch_all(query, params)
        return rows[0] if rows else None

    # ── Schema ────────────────────────────────────────────────────────────────

    def ensure_tables(self) -> None:
        for ddl in _DDL:
            try:
                self.execute(ddl)
            except Exception as e:
                log.warning("ensure_tables ddl error: %s", str(e)[:120])
        for stmt in _ALTER_STMTS:
            try:
                self.execute(stmt)
            except Exception as e:
                log.warning("ensure_tables alter error: %s", str(e)[:120])

    # ── Change store reads (called from ChangeStore refresh) ──────────────────

    def get_routing_policies(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT policy_id, agent_role, system_id, model_match, "
                "target_model, target_backend, reason, "
                "fallback_model, fallback_backend, updated_at "
                "FROM otel.gateway_routing_policies FINAL "
                "WHERE enabled = 1 ORDER BY updated_at DESC"
            )
        except Exception:
            return []

    def get_prompt_mods(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT mod_id, agent_role, system_id, mod_type, content, evidence_delta "
                "FROM otel.gateway_prompt_mods FINAL "
                "WHERE enabled = 1 ORDER BY agent_role, mod_type"
            )
        except Exception:
            return []

    def get_shadow_rules(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT rule_id, agent_role, system_id, shadow_model, shadow_backend, sample_rate "
                "FROM otel.gateway_shadow_rules FINAL "
                "WHERE enabled = 1"
            )
        except Exception:
            return []

    # ── Call log ──────────────────────────────────────────────────────────────

    def log_call(self, record: dict) -> None:
        try:
            started_at = record.get("started_at") or datetime.now(timezone.utc)
            ended_at   = record.get("ended_at")   or datetime.now(timezone.utc)
            self.execute(
                "INSERT INTO otel.gateway_call_log "
                "(call_id, trace_id, session_id, system_id, agent_role, "
                "model_requested, model_used, backend_used, routing_reason, "
                "mods_applied, prompt_text, response_text, "
                "tokens_in, tokens_out, latency_ms, is_shadow, "
                "enforcement_result, status, run_id, ab_test_id, ab_variant, key_id, "
                "cache_hit, fallback_used, conversation_id, protocol, "
                "started_at, ended_at, messages_json, response_tool_calls_json) VALUES",
                [(
                    record.get("call_id",  str(uuid.uuid4())),
                    record.get("trace_id", ""),
                    record.get("session_id", ""),
                    record.get("system_id",  ""),
                    record.get("agent_role", ""),
                    record.get("model_requested", ""),
                    record.get("model_used",      ""),
                    record.get("backend_used",    "openai"),
                    record.get("routing_reason",  ""),
                    record.get("mods_applied",    []),
                    record.get("prompt_text",     "")[:4000],
                    record.get("response_text",   "")[:4000],
                    int(record.get("tokens_in",   0)),
                    int(record.get("tokens_out",  0)),
                    int(record.get("latency_ms",  0)),
                    1 if record.get("is_shadow") else 0,
                    record.get("enforcement_result", "pass"),
                    record.get("status", "ok"),
                    record.get("run_id", ""),
                    record.get("ab_test_id", ""),
                    record.get("ab_variant",  ""),
                    record.get("key_id", ""),
                    int(record.get("cache_hit",     0)),
                    int(record.get("fallback_used", 0)),
                    record.get("conversation_id", ""),
                    record.get("protocol", "openai.chat"),
                    started_at,
                    ended_at,
                    (record.get("messages_json", "") or "")[:32000],
                    (record.get("response_tool_calls_json", "") or "")[:8000],
                )],
            )
        except Exception as e:
            log.warning("log_call failed: %s", e)

    # ── Structural events (checkpoint / handoff / tool_span) ─────────────────
    # Same front door as LLM calls.

    def log_structural_event(
        self,
        call_type:       str,
        conversation_id: str = "",
        run_id:          str = "",
        system_id:       str = "",
        agent_role:      str = "",
        payload:         Optional[dict] = None,
    ) -> str:
        event_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gateway_structural_events "
                "(event_id, call_type, conversation_id, run_id, system_id, "
                "agent_role, payload_json) VALUES",
                [(
                    event_id, call_type, conversation_id, run_id,
                    system_id, agent_role,
                    json.dumps(payload or {}, ensure_ascii=False)[:32000],
                )],
            )
        except Exception as e:
            log.warning("log_structural_event failed: %s", e)
        return event_id

    def get_structural_events(
        self, call_type: str = "", conversation_id: str = "", hours: int = 24, limit: int = 200
    ) -> list[dict]:
        conditions = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {"limit": limit}
        if call_type:
            conditions.append("call_type = %(ct)s")
            params["ct"] = call_type
        if conversation_id:
            conditions.append("conversation_id = %(cid)s")
            params["cid"] = conversation_id
        where = "WHERE " + " AND ".join(conditions)
        try:
            return self.fetch_all(
                f"SELECT event_id, call_type, conversation_id, run_id, system_id, "
                f"agent_role, payload_json, created_at "
                f"FROM otel.gateway_structural_events {where} "
                f"ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
        except Exception:
            return []

    def log_key_event(
        self,
        event_type: str,
        http_status: int,
        detail: str,
        key_id: str = "",
        key_prefix: str = "",
        agent_role: str = "",
        system_id: str = "",
        model_requested: str = "",
    ) -> None:
        try:
            self.execute(
                "INSERT INTO otel.gateway_key_events "
                "(key_id, key_prefix, agent_role, system_id, event_type, "
                "http_status, model_requested, detail) VALUES",
                [(key_id, key_prefix, agent_role, system_id, event_type,
                  http_status, model_requested, detail)],
            )
        except Exception as e:
            log.warning("log_key_event failed: %s", e)

    def get_key_events(self, key_id: str = "", limit: int = 200) -> list[dict]:
        try:
            where = "WHERE key_id = %(kid)s" if key_id else ""
            params = {"kid": key_id, "limit": limit}
            return self.fetch_all(
                f"SELECT ts, key_id, key_prefix, agent_role, system_id, "
                f"event_type, http_status, model_requested, detail "
                f"FROM otel.gateway_key_events {where} "
                f"ORDER BY ts DESC LIMIT %(limit)s",
                params,
            )
        except Exception as e:
            log.warning("get_key_events failed: %s", e)
            return []

    def get_daily_cost_usd(self) -> float:
        """Rough daily cost estimate across all non-shadow calls (for global budget alert)."""
        try:
            row = self.fetch_one(
                "SELECT sum(tokens_in + tokens_out) AS total_tokens "
                "FROM otel.gateway_call_log "
                "WHERE created_at >= toStartOfDay(now()) AND is_shadow = 0 AND cache_hit = 0"
            ) or {}
            tokens = int(row.get("total_tokens", 0) or 0)
            return tokens * 0.000001   # rough blended rate
        except Exception:
            return 0.0

    def get_call_log(
        self,
        limit: int = 100,
        system_id: str = "",
        agent_role: str = "",
        hours: int = 24,
    ) -> list[dict]:
        conditions: list[str] = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {"limit": limit}
        if system_id:
            conditions.append("system_id = %(system_id)s")
            params["system_id"] = system_id
        if agent_role:
            conditions.append("agent_role = %(agent_role)s")
            params["agent_role"] = agent_role
        where = "WHERE " + " AND ".join(conditions)
        try:
            return self.fetch_all(
                f"SELECT call_id, trace_id, system_id, agent_role, "
                f"model_requested, model_used, backend_used, routing_reason, "
                f"mods_applied, prompt_text, response_text, "
                f"tokens_in, tokens_out, latency_ms, is_shadow, "
                f"enforcement_result, status, run_id, ab_test_id, ab_variant, key_id, "
                f"cache_hit, fallback_used, conversation_id, protocol, created_at "
                f"FROM otel.gateway_call_log {where} "
                f"ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
        except Exception:
            return []

    def get_call_stats(self, hours: int = 24) -> dict:
        import math

        def _safe(v) -> float:
            if v is None:
                return 0.0
            try:
                f = float(v)
                return 0.0 if (math.isnan(f) or math.isinf(f)) else f
            except (TypeError, ValueError):
                return 0.0

        try:
            row = self.fetch_one(
                f"SELECT count() AS total_calls, "
                f"countIf(status='ok') AS ok_calls, "
                f"countIf(status='error') AS error_calls, "
                f"countIf(enforcement_result='blocked') AS blocked_calls, "
                f"sum(tokens_in + tokens_out) AS total_tokens, "
                f"avg(latency_ms) AS avg_latency_ms, "
                f"countIf(cache_hit=1) AS cache_hits, "
                f"countIf(fallback_used=1) AS fallback_count, "
                f"round(sum(tokens_in)*0.00000015 + sum(tokens_out)*0.00000060, 6) AS estimated_cost_usd "
                f"FROM otel.gateway_call_log "
                f"WHERE created_at >= now() - INTERVAL {int(hours)} HOUR "
                f"AND is_shadow = 0"
            ) or {}
            return {k: _safe(v) for k, v in row.items()}
        except Exception:
            return {}

    # ── Proposed changes ──────────────────────────────────────────────────────

    def propose_change(
        self,
        change_type: str,
        agent_role: str,
        system_id: str,
        description: str,
        payload: str,
        evidence: str,
        proposed_by: str = "reflection-agent",
    ) -> str:
        change_id = str(uuid.uuid4())
        try:
            self.execute(
                "INSERT INTO otel.gateway_proposed_changes "
                "(change_id, change_type, agent_role, system_id, "
                "description, payload, evidence, proposed_by) VALUES",
                [(change_id, change_type, agent_role, system_id,
                  description, payload, evidence, proposed_by)],
            )
        except Exception as e:
            log.warning("propose_change failed: %s", e)
        return change_id

    def decide_change(self, change_id: str, status: str, approved_by: str = "") -> None:
        existing = self.fetch_one(
            "SELECT change_type, agent_role, system_id, description, "
            "payload, evidence, proposed_by, proposed_at "
            "FROM otel.gateway_proposed_changes FINAL WHERE change_id = %(cid)s",
            {"cid": change_id},
        ) or {}
        now = datetime.now(timezone.utc)
        try:
            self.execute(
                "INSERT INTO otel.gateway_proposed_changes "
                "(change_id, change_type, agent_role, system_id, description, "
                "payload, evidence, status, approved_by, proposed_at, decided_at) VALUES",
                [(
                    change_id,
                    existing.get("change_type", ""),
                    existing.get("agent_role",  "*"),
                    existing.get("system_id",   "*"),
                    existing.get("description", ""),
                    existing.get("payload",     ""),
                    existing.get("evidence",    ""),
                    status,
                    approved_by,
                    existing.get("proposed_at", now),
                    now,
                )],
            )
        except Exception as e:
            log.warning("decide_change failed: %s", e)

    def auto_apply_change(self, change_id: str) -> Optional[dict]:
        """Read the payload of an approved change and create the corresponding policy/rule/mod.

        Returns a dict describing what was created, or None if the change_id is unknown.
        Returns {"type": "no_action", ...} for unrecognised change types.
        """
        row = self.fetch_one(
            "SELECT change_type, agent_role, system_id, payload "
            "FROM otel.gateway_proposed_changes FINAL WHERE change_id = %(cid)s",
            {"cid": change_id},
        )
        if not row:
            return None
        change_type = row.get("change_type", "")
        agent_role  = row.get("agent_role", "*")
        system_id   = row.get("system_id",  "*")
        try:
            payload = json.loads(row.get("payload", "{}") or "{}")
        except Exception:
            payload = {}

        try:
            if change_type in ("routing_override", "model_downgrade", "model_upgrade"):
                target_model   = payload.get("target_model", "")
                target_backend = payload.get("target_backend", "openai")
                model_match    = payload.get("model_match", "")
                reason         = payload.get("reason",
                                             f"auto-applied from change {change_id[:8]}")
                if target_model:
                    disabled = self.disable_routing_policies_for_role(agent_role, system_id)
                    self.insert_routing_policy(
                        agent_role, system_id, model_match,
                        target_model, target_backend, reason,
                    )
                    return {"type": "routing_policy", "target_model": target_model,
                            "replaced": disabled}

            elif change_type == "shadow_rule":
                shadow_model   = payload.get("shadow_model", "")
                shadow_backend = payload.get("shadow_backend", "openai")
                sample_rate    = float(payload.get("sample_rate", 0.1))
                if shadow_model:
                    self.insert_shadow_rule(agent_role, system_id,
                                            shadow_model, shadow_backend, sample_rate)
                    return {"type": "shadow_rule", "shadow_model": shadow_model}

            elif change_type in ("prompt_modification", "prompt_prefix_add", "few_shot_add"):
                mod_type       = payload.get("mod_type", "system_prefix")
                content        = payload.get("content", "")
                evidence_delta = float(payload.get("evidence_delta", 0.0))
                if content:
                    self.insert_prompt_mod(agent_role, system_id,
                                           mod_type, content, evidence_delta)
                    return {"type": "prompt_mod", "mod_type": mod_type}

        except Exception as e:
            log.warning("auto_apply_change failed for %s (%s): %s", change_id, change_type, e)
            return {"type": "error", "detail": str(e)}

        return {"type": "no_action", "change_type": change_type}

    def get_proposed_changes(self, status: str = "pending") -> list[dict]:
        try:
            params: dict = {}
            where = ""
            if status:
                where = "WHERE status = %(status)s"
                params["status"] = status
            return self.fetch_all(
                f"SELECT change_id, change_type, agent_role, system_id, "
                f"description, payload, evidence, status, proposed_by, "
                f"proposed_at, decided_at "
                f"FROM otel.gateway_proposed_changes FINAL {where} "
                f"ORDER BY proposed_at DESC",
                params,
            )
        except Exception:
            return []

    # ── Policy writes (from operations.py) ───────────────────────────────────

    def insert_routing_policy(
        self,
        agent_role: str,
        system_id: str,
        model_match: str,
        target_model: str,
        target_backend: str,
        reason: str,
        fallback_model: str = "",
        fallback_backend: str = "openai",
    ) -> None:
        self.execute(
            "INSERT INTO otel.gateway_routing_policies "
            "(agent_role, system_id, model_match, target_model, target_backend, "
            "reason, fallback_model, fallback_backend) VALUES",
            [(agent_role, system_id, model_match, target_model, target_backend,
              reason, fallback_model, fallback_backend)],
        )

    def disable_routing_policies_for_role(self, agent_role: str, system_id: str) -> int:
        """Disable all enabled routing policies for a given (agent_role, system_id).

        Called by auto_apply_change before inserting a replacement so old policies
        do not accumulate and the new policy takes effect cleanly.
        Returns the number of policies disabled.
        """
        rows = self.fetch_all(
            "SELECT policy_id, agent_role, system_id, model_match, target_model, "
            "target_backend, reason, approved_by "
            "FROM otel.gateway_routing_policies FINAL "
            "WHERE enabled = 1 AND agent_role = %(role)s AND system_id = %(sid)s",
            {"role": agent_role, "sid": system_id},
        )
        for row in rows:
            try:
                self.execute(
                    "INSERT INTO otel.gateway_routing_policies "
                    "(policy_id, agent_role, system_id, model_match, target_model, "
                    "target_backend, reason, enabled, approved_by) VALUES",
                    [(
                        row["policy_id"], row.get("agent_role", "*"), row.get("system_id", "*"),
                        row.get("model_match", ""), row.get("target_model", ""),
                        row.get("target_backend", "openai"), row.get("reason", ""),
                        0, row.get("approved_by", "system"),
                    )],
                )
            except Exception as e:
                log.warning("disable_routing_policies_for_role error: %s", e)
        return len(rows)

    def update_routing_policy(
        self,
        policy_id: str,
        model_match: str,
        target_model: str,
        target_backend: str,
        reason: str,
        fallback_model: str = "",
        fallback_backend: str = "openai",
    ) -> None:
        row = self.fetch_one(
            "SELECT agent_role, system_id, approved_by "
            "FROM otel.gateway_routing_policies FINAL WHERE policy_id = %(pid)s",
            {"pid": policy_id},
        ) or {}
        self.execute(
            "INSERT INTO otel.gateway_routing_policies "
            "(policy_id, agent_role, system_id, model_match, target_model, "
            "target_backend, reason, fallback_model, fallback_backend, enabled, approved_by) VALUES",
            [(
                policy_id, row.get("agent_role", "*"), row.get("system_id", "*"),
                model_match, target_model, target_backend, reason,
                fallback_model, fallback_backend,
                1, row.get("approved_by", "operator"),
            )],
        )

    def disable_routing_policy(self, policy_id: str) -> None:
        safe = policy_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT policy_id, agent_role, system_id, model_match, target_model, "
            f"target_backend, reason, approved_by "
            f"FROM otel.gateway_routing_policies FINAL WHERE policy_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_routing_policies "
            "(policy_id, agent_role, system_id, model_match, target_model, "
            "target_backend, reason, enabled, approved_by) VALUES",
            [(
                row["policy_id"], row.get("agent_role", "*"), row.get("system_id", "*"),
                row.get("model_match", ""), row.get("target_model", ""),
                row.get("target_backend", "openai"), row.get("reason", ""),
                0, row.get("approved_by", "system"),
            )],
        )

    def insert_prompt_mod(
        self,
        agent_role: str,
        system_id: str,
        mod_type: str,
        content: str,
        evidence_delta: float,
    ) -> None:
        self.execute(
            "INSERT INTO otel.gateway_prompt_mods "
            "(agent_role, system_id, mod_type, content, evidence_delta) VALUES",
            [(agent_role, system_id, mod_type, content, float(evidence_delta))],
        )

    def update_prompt_mod(
        self,
        mod_id: str,
        mod_type: str,
        content: str,
        evidence_delta: float,
    ) -> None:
        row = self.fetch_one(
            "SELECT agent_role, system_id, approved_by "
            "FROM otel.gateway_prompt_mods FINAL WHERE mod_id = %(mid)s",
            {"mid": mod_id},
        ) or {}
        self.execute(
            "INSERT INTO otel.gateway_prompt_mods "
            "(mod_id, agent_role, system_id, mod_type, content, "
            "evidence_delta, enabled, approved_by) VALUES",
            [(
                mod_id, row.get("agent_role", "*"), row.get("system_id", "*"),
                mod_type, content, float(evidence_delta),
                1, row.get("approved_by", "operator"),
            )],
        )

    def disable_prompt_mod(self, mod_id: str) -> None:
        safe = mod_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT mod_id, agent_role, system_id, mod_type, content, "
            f"evidence_delta, approved_by "
            f"FROM otel.gateway_prompt_mods FINAL WHERE mod_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_prompt_mods "
            "(mod_id, agent_role, system_id, mod_type, content, "
            "evidence_delta, enabled, approved_by) VALUES",
            [(
                row["mod_id"], row.get("agent_role", "*"), row.get("system_id", "*"),
                row.get("mod_type", "system_prefix"), row.get("content", ""),
                float(row.get("evidence_delta", 0.0)), 0,
                row.get("approved_by", "system"),
            )],
        )

    def insert_shadow_rule(
        self,
        agent_role: str,
        system_id: str,
        shadow_model: str,
        shadow_backend: str,
        sample_rate: float,
    ) -> None:
        self.execute(
            "INSERT INTO otel.gateway_shadow_rules "
            "(agent_role, system_id, shadow_model, shadow_backend, sample_rate) VALUES",
            [(agent_role, system_id, shadow_model, shadow_backend, float(sample_rate))],
        )

    def update_shadow_rule(
        self,
        rule_id: str,
        shadow_model: str,
        shadow_backend: str,
        sample_rate: float,
    ) -> None:
        row = self.fetch_one(
            "SELECT agent_role, system_id "
            "FROM otel.gateway_shadow_rules FINAL WHERE rule_id = %(rid)s",
            {"rid": rule_id},
        ) or {}
        self.execute(
            "INSERT INTO otel.gateway_shadow_rules "
            "(rule_id, agent_role, system_id, shadow_model, shadow_backend, sample_rate, enabled) VALUES",
            [(
                rule_id, row.get("agent_role", "*"), row.get("system_id", "*"),
                shadow_model, shadow_backend, float(sample_rate), 1,
            )],
        )

    # ── Pipeline shadow evals ─────────────────────────────────────────────────

    def log_pipeline_shadow_eval(self, data: dict) -> None:
        try:
            self.execute(
                "INSERT INTO otel.gateway_pipeline_shadow_evals "
                "(agent_role, user_input, primary_model, shadow_model, "
                "primary_response, shadow_response, "
                "primary_tokens, shadow_tokens, primary_latency_ms, shadow_latency_ms, "
                "primary_faithfulness, primary_relevance, primary_instruction, "
                "shadow_faithfulness, shadow_relevance, shadow_instruction) VALUES",
                [(
                    data.get("agent_role", ""),
                    data.get("user_input", "")[:2000],
                    data.get("primary_model", ""),
                    data.get("shadow_model", ""),
                    data.get("primary_response", "")[:4000],
                    data.get("shadow_response", "")[:4000],
                    int(data.get("primary_tokens", 0)),
                    int(data.get("shadow_tokens", 0)),
                    int(data.get("primary_latency_ms", 0)),
                    int(data.get("shadow_latency_ms", 0)),
                    float(data.get("primary_faithfulness", 0.0)),
                    float(data.get("primary_relevance", 0.0)),
                    float(data.get("primary_instruction", 0.0)),
                    float(data.get("shadow_faithfulness", 0.0)),
                    float(data.get("shadow_relevance", 0.0)),
                    float(data.get("shadow_instruction", 0.0)),
                )],
            )
        except Exception as e:
            log.warning("log_pipeline_shadow_eval failed: %s", e)

    def get_pipeline_shadow_evals(
        self, agent_role: str = "", hours: int = 168, limit: int = 100
    ) -> list[dict]:
        where_parts = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {"limit": limit}
        if agent_role:
            where_parts.append("agent_role = %(role)s")
            params["role"] = agent_role
        where = "WHERE " + " AND ".join(where_parts)
        try:
            return self.fetch_all(
                f"SELECT eval_id, agent_role, user_input, primary_model, shadow_model, "
                f"primary_response, shadow_response, "
                f"primary_tokens, shadow_tokens, primary_latency_ms, shadow_latency_ms, "
                f"primary_faithfulness, primary_relevance, primary_instruction, "
                f"shadow_faithfulness, shadow_relevance, shadow_instruction, created_at "
                f"FROM otel.gateway_pipeline_shadow_evals {where} "
                f"ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
        except Exception:
            return []

    def get_pipeline_shadow_summary(
        self, agent_role: str = "", hours: int = 168
    ) -> list[dict]:
        where_parts = [f"created_at >= now() - INTERVAL {int(hours)} HOUR"]
        params: dict = {}
        if agent_role:
            where_parts.append("agent_role = %(role)s")
            params["role"] = agent_role
        where = "WHERE " + " AND ".join(where_parts)
        try:
            return self.fetch_all(
                f"SELECT agent_role, primary_model, shadow_model, "
                f"count() AS evals, "
                f"round(avg(primary_faithfulness), 3) AS avg_pri_faith, "
                f"round(avg(primary_relevance),    3) AS avg_pri_rel, "
                f"round(avg(primary_instruction),  3) AS avg_pri_inst, "
                f"round(avg(shadow_faithfulness),  3) AS avg_shad_faith, "
                f"round(avg(shadow_relevance),     3) AS avg_shad_rel, "
                f"round(avg(shadow_instruction),   3) AS avg_shad_inst, "
                f"round(avg(primary_latency_ms),   0) AS avg_pri_latency_ms, "
                f"round(avg(shadow_latency_ms),    0) AS avg_shad_latency_ms "
                f"FROM otel.gateway_pipeline_shadow_evals {where} "
                f"GROUP BY agent_role, primary_model, shadow_model "
                f"ORDER BY agent_role, evals DESC",
                params,
            )
        except Exception:
            return []

    def disable_shadow_rule(self, rule_id: str) -> None:
        safe = rule_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT rule_id, agent_role, system_id, shadow_model, "
            f"shadow_backend, sample_rate "
            f"FROM otel.gateway_shadow_rules FINAL WHERE rule_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_shadow_rules "
            "(rule_id, agent_role, system_id, shadow_model, "
            "shadow_backend, sample_rate, enabled) VALUES",
            [(
                row["rule_id"], row.get("agent_role", "*"), row.get("system_id", "*"),
                row.get("shadow_model", ""), row.get("shadow_backend", "openai"),
                float(row.get("sample_rate", 0.1)), 0,
            )],
        )

    # ── A/B tests ─────────────────────────────────────────────────────────────

    def get_ab_tests(self, status: str = "") -> list[dict]:
        where = "WHERE status = %(status)s" if status else ""
        params: dict = {}
        if status:
            params["status"] = status
        try:
            return self.fetch_all(
                f"SELECT test_id, test_name, agent_role, system_id, "
                f"variant_a_model, variant_a_backend, variant_a_prompt, "
                f"variant_b_model, variant_b_backend, variant_b_prompt, "
                f"split_ratio, status, created_at, updated_at "
                f"FROM otel.gateway_ab_tests FINAL {where} "
                f"ORDER BY updated_at DESC",
                params,
            )
        except Exception:
            return []

    def insert_ab_test(
        self,
        test_name:         str,
        agent_role:        str,
        system_id:         str,
        variant_a_model:   str,
        variant_a_backend: str,
        variant_a_prompt:  str,
        variant_b_model:   str,
        variant_b_backend: str,
        variant_b_prompt:  str,
        split_ratio:       float,
    ) -> str:
        test_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO otel.gateway_ab_tests "
            "(test_id, test_name, agent_role, system_id, "
            "variant_a_model, variant_a_backend, variant_a_prompt, "
            "variant_b_model, variant_b_backend, variant_b_prompt, "
            "split_ratio, status) VALUES",
            [(
                test_id, test_name, agent_role, system_id,
                variant_a_model, variant_a_backend, variant_a_prompt,
                variant_b_model, variant_b_backend, variant_b_prompt,
                float(split_ratio), "draft",
            )],
        )
        return test_id

    def update_ab_test_status(self, test_id: str, status: str) -> None:
        safe = test_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT test_id, test_name, agent_role, system_id, "
            f"variant_a_model, variant_a_backend, variant_a_prompt, "
            f"variant_b_model, variant_b_backend, variant_b_prompt, split_ratio "
            f"FROM otel.gateway_ab_tests FINAL WHERE test_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_ab_tests "
            "(test_id, test_name, agent_role, system_id, "
            "variant_a_model, variant_a_backend, variant_a_prompt, "
            "variant_b_model, variant_b_backend, variant_b_prompt, "
            "split_ratio, status) VALUES",
            [(
                row["test_id"], row.get("test_name", ""),
                row.get("agent_role", "*"), row.get("system_id", "*"),
                row.get("variant_a_model", ""), row.get("variant_a_backend", "openai"),
                row.get("variant_a_prompt", ""),
                row.get("variant_b_model", ""), row.get("variant_b_backend", "openai"),
                row.get("variant_b_prompt", ""),
                float(row.get("split_ratio", 0.5)), status,
            )],
        )

    def delete_ab_test(self, test_id: str) -> None:
        self.update_ab_test_status(test_id, "deleted")

    # ── API key management ────────────────────────────────────────────────────

    def create_api_key(
        self,
        description:       str,
        agent_role:        str   = "*",
        system_id:         str   = "*",
        allowed_models:    list  = None,
        daily_token_limit: int   = 0,
        is_admin:          bool  = False,
        rate_limit_rpm:    int   = 0,
        budget_alert_usd:  float = 0.0,
        alert_webhook_url: str   = "",
    ) -> tuple[str, str]:
        import hashlib, secrets
        raw_key  = "gw-sk-" + secrets.token_hex(16)
        key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
        key_id   = str(uuid.uuid4())
        self.execute(
            "INSERT INTO otel.gateway_api_keys "
            "(key_id, key_hash, key_prefix, description, agent_role, system_id, "
            "allowed_models, daily_token_limit, is_admin, "
            "rate_limit_rpm, budget_alert_usd, alert_webhook_url) VALUES",
            [(
                key_id, key_hash, raw_key[:12], description,
                agent_role, system_id,
                allowed_models or [],
                int(daily_token_limit),
                1 if is_admin else 0,
                int(rate_limit_rpm),
                float(budget_alert_usd),
                alert_webhook_url,
            )],
        )
        return key_id, raw_key

    def validate_api_key(self, key_hash: str) -> Optional[dict]:
        return self.fetch_one(
            "SELECT key_id, key_prefix, description, agent_role, system_id, "
            "allowed_models, daily_token_limit, is_admin, enabled, "
            "rate_limit_rpm, budget_alert_usd, alert_webhook_url "
            "FROM otel.gateway_api_keys FINAL "
            "WHERE key_hash = %(kh)s",
            {"kh": key_hash},
        )

    def list_api_keys(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT key_id, key_prefix, description, agent_role, system_id, "
                "allowed_models, daily_token_limit, is_admin, enabled, created_at, "
                "rate_limit_rpm, budget_alert_usd, alert_webhook_url "
                "FROM otel.gateway_api_keys FINAL "
                "WHERE enabled = 1 ORDER BY created_at DESC"
            )
        except Exception:
            return []

    def disable_api_key(self, key_id: str) -> None:
        safe = key_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT key_id, key_hash, key_prefix, description, agent_role, system_id, "
            f"allowed_models, daily_token_limit, is_admin, "
            f"rate_limit_rpm, budget_alert_usd, alert_webhook_url "
            f"FROM otel.gateway_api_keys FINAL WHERE key_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_api_keys "
            "(key_id, key_hash, key_prefix, description, agent_role, system_id, "
            "allowed_models, daily_token_limit, is_admin, enabled, "
            "rate_limit_rpm, budget_alert_usd, alert_webhook_url) VALUES",
            [(
                row["key_id"], row.get("key_hash", ""), row.get("key_prefix", ""),
                row.get("description", ""), row.get("agent_role", "*"),
                row.get("system_id", "*"), row.get("allowed_models", []),
                int(row.get("daily_token_limit", 0)),
                int(row.get("is_admin", 0)), 0,
                int(row.get("rate_limit_rpm", 0)),
                float(row.get("budget_alert_usd", 0.0)),
                row.get("alert_webhook_url", ""),
            )],
        )

    def update_api_key(self, key_id: str, updates: dict) -> bool:
        safe = key_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT key_id, key_hash, key_prefix, description, agent_role, system_id, "
            f"allowed_models, daily_token_limit, is_admin, enabled, "
            f"rate_limit_rpm, budget_alert_usd, alert_webhook_url "
            f"FROM otel.gateway_api_keys FINAL WHERE key_id = '{safe}' AND enabled = 1"
        ) or {}
        if not row:
            return False
        self.execute(
            "INSERT INTO otel.gateway_api_keys "
            "(key_id, key_hash, key_prefix, description, agent_role, system_id, "
            "allowed_models, daily_token_limit, is_admin, enabled, "
            "rate_limit_rpm, budget_alert_usd, alert_webhook_url) VALUES",
            [(
                row["key_id"], row.get("key_hash", ""), row.get("key_prefix", ""),
                updates.get("description",       row.get("description", "")),
                updates.get("agent_role",        row.get("agent_role", "*")),
                updates.get("system_id",         row.get("system_id", "*")),
                updates.get("allowed_models",    row.get("allowed_models", [])),
                int(updates.get("daily_token_limit", row.get("daily_token_limit", 0))),
                int(row.get("is_admin", 0)),
                int(row.get("enabled", 1)),
                int(updates.get("rate_limit_rpm",   row.get("rate_limit_rpm", 0))),
                float(updates.get("budget_alert_usd", row.get("budget_alert_usd", 0.0))),
                updates.get("alert_webhook_url", row.get("alert_webhook_url", "")),
            )],
        )
        return True

    def get_key_daily_tokens(self, key_id: str) -> int:
        safe = key_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT sum(tokens_in + tokens_out) AS total "
            f"FROM otel.gateway_call_log "
            f"WHERE key_id = '{safe}' AND created_at >= toStartOfDay(now())"
        ) or {}
        return int(row.get("total", 0) or 0)

    def get_key_usage(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT key_id, "
                "count() AS calls_today, "
                "sum(tokens_in + tokens_out) AS tokens_today "
                "FROM otel.gateway_call_log "
                "WHERE created_at >= toStartOfDay(now()) AND key_id != '' "
                "GROUP BY key_id"
            )
        except Exception:
            return []

    def get_ab_test_results(self, test_id: str) -> dict:
        safe = test_id.replace("'", "")[:64]
        try:
            call_rows = self.fetch_all(
                f"SELECT ab_variant, "
                f"count() AS call_count, "
                f"round(avg(latency_ms), 0) AS avg_latency_ms, "
                f"round(avg(tokens_in),  0) AS avg_tokens_in, "
                f"round(avg(tokens_out), 0) AS avg_tokens_out, "
                f"sum(tokens_in + tokens_out) AS total_tokens "
                f"FROM otel.gateway_call_log "
                f"WHERE ab_test_id = '{safe}' AND ab_variant != '' "
                f"GROUP BY ab_variant ORDER BY ab_variant"
            )
        except Exception:
            call_rows = []
        try:
            score_rows = self.fetch_all(
                f"SELECT c.ab_variant, "
                f"round(avg(p.scores['faithfulness']),          3) AS avg_faithfulness, "
                f"round(avg(p.scores['relevance']),             3) AS avg_relevance, "
                f"round(avg(p.scores['instruction_following']), 3) AS avg_instruction, "
                f"count() AS eval_count "
                f"FROM otel.gateway_call_log c "
                f"INNER JOIN otel.prompt_evals p ON c.trace_id = p.trace_id "
                f"WHERE c.ab_test_id = '{safe}' AND c.ab_variant != '' "
                f"AND (p.scores['faithfulness'] > 0 OR p.scores['relevance'] > 0) "
                f"GROUP BY c.ab_variant ORDER BY c.ab_variant"
            )
        except Exception:
            score_rows = []
        return {"call_stats": call_rows, "eval_scores": score_rows}

    # ── Traffic management ────────────────────────────────────────────────────

    def get_endpoint_pools(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT pool_id, name, strategy, description, enabled, created_at, updated_at "
                "FROM otel.gateway_endpoint_pools FINAL "
                "WHERE enabled = 1 ORDER BY created_at DESC"
            )
        except Exception:
            return []

    def get_pool_endpoints(self, pool_id: str = "") -> list[dict]:
        try:
            where = f"WHERE pool_id = %(pid)s AND enabled = 1" if pool_id else "WHERE enabled = 1"
            params = {"pid": pool_id} if pool_id else {}
            return self.fetch_all(
                f"SELECT endpoint_id, pool_id, model, backend, weight, priority, enabled, created_at "
                f"FROM otel.gateway_pool_endpoints FINAL {where} "
                f"ORDER BY pool_id, priority, weight DESC",
                params,
            )
        except Exception:
            return []

    def get_traffic_policies(self) -> list[dict]:
        try:
            return self.fetch_all(
                "SELECT policy_id, agent_role, system_id, pool_id, sticky, enabled, created_at "
                "FROM otel.gateway_traffic_policies FINAL "
                "WHERE enabled = 1 ORDER BY created_at DESC"
            )
        except Exception:
            return []

    def insert_endpoint_pool(
        self, name: str, strategy: str, description: str = ""
    ) -> str:
        pool_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO otel.gateway_endpoint_pools "
            "(pool_id, name, strategy, description) VALUES",
            [(pool_id, name, strategy, description)],
        )
        return pool_id

    def disable_endpoint_pool(self, pool_id: str) -> None:
        safe = pool_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT pool_id, name, strategy, description "
            f"FROM otel.gateway_endpoint_pools FINAL WHERE pool_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_endpoint_pools "
            "(pool_id, name, strategy, description, enabled) VALUES",
            [(row["pool_id"], row.get("name", ""), row.get("strategy", "round_robin"),
              row.get("description", ""), 0)],
        )

    def insert_pool_endpoint(
        self,
        pool_id: str,
        model: str,
        backend: str = "openai",
        weight: float = 1.0,
        priority: int = 1,
    ) -> str:
        endpoint_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO otel.gateway_pool_endpoints "
            "(endpoint_id, pool_id, model, backend, weight, priority) VALUES",
            [(endpoint_id, pool_id, model, backend, float(weight), int(priority))],
        )
        return endpoint_id

    def disable_pool_endpoint(self, endpoint_id: str) -> None:
        safe = endpoint_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT endpoint_id, pool_id, model, backend, weight, priority "
            f"FROM otel.gateway_pool_endpoints FINAL WHERE endpoint_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_pool_endpoints "
            "(endpoint_id, pool_id, model, backend, weight, priority, enabled) VALUES",
            [(
                row["endpoint_id"], row.get("pool_id", ""), row.get("model", ""),
                row.get("backend", "openai"), float(row.get("weight", 1.0)),
                int(row.get("priority", 1)), 0,
            )],
        )

    def insert_traffic_policy(
        self,
        agent_role: str,
        system_id: str,
        pool_id: str,
        sticky: bool = False,
    ) -> str:
        policy_id = str(uuid.uuid4())
        self.execute(
            "INSERT INTO otel.gateway_traffic_policies "
            "(policy_id, agent_role, system_id, pool_id, sticky) VALUES",
            [(policy_id, agent_role, system_id, pool_id, 1 if sticky else 0)],
        )
        return policy_id

    def disable_traffic_policy(self, policy_id: str) -> None:
        safe = policy_id.replace("'", "")[:64]
        row = self.fetch_one(
            f"SELECT policy_id, agent_role, system_id, pool_id, sticky "
            f"FROM otel.gateway_traffic_policies FINAL WHERE policy_id = '{safe}'"
        ) or {}
        if not row:
            return
        self.execute(
            "INSERT INTO otel.gateway_traffic_policies "
            "(policy_id, agent_role, system_id, pool_id, sticky, enabled) VALUES",
            [(
                row["policy_id"], row.get("agent_role", "*"), row.get("system_id", "*"),
                row.get("pool_id", ""), int(row.get("sticky", 0)), 0,
            )],
        )

    def get_pool_endpoint_stats(
        self, agent_role: str, models: list[str]
    ) -> dict[str, dict]:
        """Return per-model latency and quality stats for endpoint selection.

        Queries gateway_call_log (last 5 min) for latency/error rate,
        and gateway_shadow_evals (last 1h) for faithfulness.
        Returns {model: {avg_latency_ms, error_rate, avg_faithfulness}}.
        """
        if not models:
            return {}
        model_list = ", ".join(f"'{m.replace(chr(39), '')}'" for m in models)
        stats: dict[str, dict] = {m: {"avg_latency_ms": 9999.0, "error_rate": 0.0,
                                       "avg_faithfulness": 0.0} for m in models}
        try:
            latency_rows = self.fetch_all(
                f"SELECT model_used, "
                f"round(avg(latency_ms), 1) AS avg_latency_ms, "
                f"round(countIf(status != 'ok') / count(), 4) AS error_rate "
                f"FROM otel.gateway_call_log "
                f"WHERE created_at >= now() - INTERVAL 5 MINUTE "
                f"AND agent_role = %(role)s "
                f"AND model_used IN ({model_list}) "
                f"GROUP BY model_used",
                {"role": agent_role},
            )
            for row in latency_rows:
                m = row["model_used"]
                if m in stats:
                    stats[m]["avg_latency_ms"] = float(row.get("avg_latency_ms", 9999))
                    stats[m]["error_rate"]      = float(row.get("error_rate", 0))
        except Exception:
            pass
        try:
            faith_rows = self.fetch_all(
                f"SELECT model_used, round(avg(scores['faithfulness']), 3) AS avg_faithfulness "
                f"FROM otel.gateway_shadow_evals "
                f"WHERE scored_at >= now() - INTERVAL 1 HOUR "
                f"AND agent_role = %(role)s "
                f"AND model_used IN ({model_list}) "
                f"AND scores['faithfulness'] > 0 "
                f"GROUP BY model_used",
                {"role": agent_role},
            )
            for row in faith_rows:
                m = row["model_used"]
                if m in stats:
                    stats[m]["avg_faithfulness"] = float(row.get("avg_faithfulness", 0))
        except Exception:
            pass
        return stats
