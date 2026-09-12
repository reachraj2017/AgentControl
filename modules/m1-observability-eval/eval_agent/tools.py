"""Tool implementations and schemas for the M1 Eval Agent."""

import json
import os
from typing import Any

import httpx
import structlog
from clickhouse_driver import Client

log = structlog.get_logger()

EVAL_RUNNER_URL = os.getenv("EVAL_RUNNER_URL", "http://localhost:8000")
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

    def _get_token_rates(self) -> tuple[float, float]:
        try:
            rates = {
                r["config_key"]: float(r["value"])
                for r in self._run(
                    "SELECT config_key, value FROM otel.gov_threshold_config FINAL "
                    "WHERE config_key IN ('budget.input_token_cost_per_1m', "
                    "                     'budget.output_token_cost_per_1m')"
                )
            }
        except Exception:
            rates = {}
        return (
            rates.get("budget.input_token_cost_per_1m", 0.15) / 1_000_000,
            rates.get("budget.output_token_cost_per_1m", 0.60) / 1_000_000,
        )

    def get_recent_traces(self, agent_role: str = "", hours: int = 720, limit: int = 20) -> list[dict]:
        h = int(hours)
        params: dict = {"limit": limit}
        role_filter = ""
        if agent_role:
            role_filter = "AND SpanAttributes['agent.role'] = %(role)s"
            params["role"] = agent_role
        rows = self._run(
            f"SELECT TraceId, SpanId, SpanName, ServiceName, "
            f"round(Duration / 1e9, 3) AS duration_s, StatusCode, "
            f"SpanAttributes['agent.role'] AS agent_role, "
            f"SpanAttributes['task.status'] AS task_status, "
            f"Timestamp "
            f"FROM otel.otel_traces "
            f"WHERE SpanName = 'agent.task' "
            f"AND SpanAttributes['agent.role'] != '' "
            f"AND Timestamp >= now() - INTERVAL {h} HOUR "
            f"{role_filter} "
            f"ORDER BY Timestamp DESC LIMIT %(limit)s",
            params,
        )
        if not rows and hours < 720:
            return self.get_recent_traces(agent_role=agent_role, hours=720, limit=limit)
        return rows

    def get_eval_scores(self, hours: int = 720) -> list[dict]:
        return self._run(
            f"SELECT metric, round(avg(score), 3) AS avg_score, "
            f"round(min(score), 3) AS min_score, round(max(score), 3) AS max_score, "
            f"count() AS sample_count "
            f"FROM otel.eval_scores "
            f"WHERE evaluated_at >= now() - INTERVAL {int(hours)} HOUR "
            f"GROUP BY metric ORDER BY metric"
        )

    def get_cost_breakdown(self, hours: int = 720, source: str = "") -> list[dict]:
        h = int(hours)
        input_rate, output_rate = self._get_token_rates()
        if source:
            rows = self._run(
                f"SELECT "
                f"  pe.agent_name AS agent_role, "
                f"  count()    AS trace_count, "
                f"  sum(pe.prompt_tokens)     AS total_input_tokens, "
                f"  sum(pe.completion_tokens) AS total_output_tokens, "
                f"  round(sum(pe.prompt_tokens) * {input_rate} + sum(pe.completion_tokens) * {output_rate}, 6) AS total_cost_usd "
                f"FROM otel.prompt_evals pe "
                f"INNER JOIN ( "
                f"  SELECT DISTINCT TraceId "
                f"  FROM otel.otel_traces "
                f"  WHERE SpanAttributes['trace.source'] = %(src)s "
                f"  AND Timestamp >= now() - INTERVAL {h} HOUR "
                f") t ON pe.trace_id = t.TraceId "
                f"WHERE pe.created_at >= now() - INTERVAL {h} HOUR "
                f"  AND pe.agent_name != '' "
                f"GROUP BY pe.agent_name "
                f"ORDER BY total_cost_usd DESC",
                {"src": source},
            )
        else:
            rows = self._run(
                f"SELECT "
                f"  agent_name AS agent_role, "
                f"  count()    AS trace_count, "
                f"  sum(prompt_tokens)     AS total_input_tokens, "
                f"  sum(completion_tokens) AS total_output_tokens, "
                f"  round(sum(prompt_tokens) * {input_rate} + sum(completion_tokens) * {output_rate}, 6) AS total_cost_usd "
                f"FROM otel.prompt_evals "
                f"WHERE created_at >= now() - INTERVAL {h} HOUR "
                f"  AND agent_name != '' "
                f"GROUP BY agent_name "
                f"ORDER BY total_cost_usd DESC"
            )
        if not rows and hours < 720:
            return self.get_cost_breakdown(hours=720, source=source)
        return rows

    def get_error_rate(self, hours: int = 168) -> list[dict]:
        h = int(hours)
        rows = self._run(
            f"SELECT SpanAttributes['agent.role'] AS agent_role, "
            f"countIf(StatusCode = 'STATUS_CODE_ERROR') AS errors, "
            f"count() AS total, "
            f"round(countIf(StatusCode = 'STATUS_CODE_ERROR') / count(), 3) AS error_rate "
            f"FROM otel.otel_traces "
            f"WHERE SpanName = 'agent.task' "
            f"AND SpanAttributes['agent.role'] != '' "
            f"AND Timestamp >= now() - INTERVAL {h} HOUR "
            f"GROUP BY agent_role ORDER BY error_rate DESC"
        )
        if not rows and hours < 720:
            return self.get_error_rate(hours=720)
        return rows

    def _enrich_conversation_ids(self, rows: list[dict]) -> None:
        trace_ids = list({r["trace_id"] for r in rows if r.get("trace_id")})
        if not trace_ids:
            return
        id_list = ", ".join(f"'{tid}'" for tid in trace_ids)
        try:
            tr = self._run(
                f"SELECT DISTINCT TraceId, "
                f"SpanAttributes['conversation.id'] AS conversation_id "
                f"FROM otel.otel_traces "
                f"WHERE TraceId IN ({id_list}) AND SpanName = 'agent.task'"
            )
            conv_map = {r["TraceId"]: r.get("conversation_id", "") for r in tr}
        except Exception:
            conv_map = {}
        for r in rows:
            r["conversation_id"] = conv_map.get(r.get("trace_id", ""), "")

    def get_prompt_detail(self, trace_id: str) -> list[dict]:
        input_rate, output_rate = self._get_token_rates()
        rows = self._run(
            "SELECT prompt_eval_id, trace_id, run_id, agent_name, model, "
            "prompt_text, response_text, prompt_tokens, completion_tokens, "
            f"round(prompt_tokens * {input_rate} + completion_tokens * {output_rate}, 6) AS cost_usd, "
            "scores, latency_ms, created_at "
            "FROM otel.prompt_evals "
            "WHERE trace_id = %(tid)s "
            "ORDER BY created_at DESC LIMIT 10",
            {"tid": trace_id},
        )
        if rows:
            trace_rows = self._run(
                "SELECT TraceId, "
                "SpanAttributes['trace.source'] AS trace_source, "
                "SpanAttributes['conversation.id'] AS conversation_id "
                "FROM otel.otel_traces "
                "WHERE TraceId = %(tid)s AND SpanName = 'agent.task' LIMIT 1",
                {"tid": trace_id},
            )
            src = trace_rows[0]["trace_source"] if trace_rows else ""
            conv_id = trace_rows[0]["conversation_id"] if trace_rows else ""
            for r in rows:
                r["source"] = src
                r["conversation_id"] = conv_id
        return rows

    def search_prompts(
        self,
        agent_name: str = "",
        source: str = "",
        conversation_id: str = "",
        hours: int = 720,
        limit: int = 20,
    ) -> list[dict]:
        h = int(hours)
        params: dict = {"limit": min(int(limit), 50)}
        needs_join = bool(source or conversation_id)
        join_conds = []
        if source:
            join_conds.append(f"SpanAttributes['trace.source'] = %(src)s")
            params["src"] = source
        if conversation_id:
            join_conds.append(f"SpanAttributes['conversation.id'] = %(conv)s")
            params["conv"] = conversation_id
        agent_filter = "AND pe.agent_name = %(agent)s" if agent_name else ""
        if agent_name:
            params["agent"] = agent_name
        if needs_join:
            join_where = " AND ".join(join_conds)
            rows = self._run(
                f"SELECT pe.prompt_eval_id, pe.trace_id, pe.run_id, pe.agent_name, pe.model, "
                f"pe.prompt_text, pe.response_text, pe.prompt_tokens, pe.completion_tokens, "
                f"pe.scores, pe.latency_ms, pe.created_at "
                f"FROM otel.prompt_evals pe "
                f"INNER JOIN ( "
                f"  SELECT DISTINCT TraceId "
                f"  FROM otel.otel_traces "
                f"  WHERE {join_where} "
                f"  AND Timestamp >= now() - INTERVAL {h} HOUR "
                f") t ON pe.trace_id = t.TraceId "
                f"WHERE pe.created_at >= now() - INTERVAL {h} HOUR "
                f"  AND pe.agent_name != '' "
                f"  {agent_filter} "
                f"ORDER BY pe.created_at DESC LIMIT %(limit)s",
                params,
            )
        else:
            conds = [f"created_at >= now() - INTERVAL {h} HOUR", "agent_name != ''"]
            if agent_name:
                conds.append("agent_name = %(agent)s")
            where = " AND ".join(conds)
            rows = self._run(
                f"SELECT prompt_eval_id, trace_id, run_id, agent_name, model, "
                f"prompt_text, response_text, prompt_tokens, completion_tokens, "
                f"scores, latency_ms, created_at "
                f"FROM otel.prompt_evals "
                f"WHERE {where} "
                f"ORDER BY created_at DESC LIMIT %(limit)s",
                params,
            )
        if not rows and hours < 720:
            return self.search_prompts(
                agent_name=agent_name, source=source,
                conversation_id=conversation_id, hours=720, limit=limit,
            )
        self._enrich_conversation_ids(rows)
        return rows

    def get_eval_runs(self, suite: str = "", hours: int = 720, limit: int = 20) -> list[dict]:
        h = int(hours)
        conds = [f"created_at >= now() - INTERVAL {h} HOUR"]
        params: dict = {"limit": min(int(limit), 100)}
        if suite:
            conds.append("suite = %(suite)s")
            params["suite"] = suite
        where = " AND ".join(conds)
        rows = self._run(
            f"SELECT toString(run_id) AS run_id, name, suite, agent_version, is_baseline, created_at "
            f"FROM otel.eval_runs "
            f"WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )
        if not rows and hours < 720:
            return self.get_eval_runs(suite=suite, hours=720, limit=limit)
        return rows

    def get_benchmarks(self, suite: str = "", difficulty: str = "", limit: int = 50) -> list[dict]:
        conds = ["active = 1"]
        params: dict = {"limit": min(int(limit), 200)}
        if suite:
            conds.append("suite = %(suite)s")
            params["suite"] = suite
        if difficulty:
            conds.append("difficulty = %(diff)s")
            params["diff"] = difficulty
        where = " AND ".join(conds)
        return self._run(
            f"SELECT toString(benchmark_id) AS benchmark_id, suite, name, task_input, expected_output, "
            f"rubric, difficulty, dataset_version, created_at "
            f"FROM otel.benchmarks "
            f"WHERE {where} "
            f"ORDER BY created_at DESC LIMIT %(limit)s",
            params,
        )

    def get_eval_scores_detail(
        self, trace_id: str = "", run_id: str = "", metric: str = "", limit: int = 20
    ) -> list[dict]:
        conds = []
        params: dict = {"limit": min(int(limit), 100)}
        if trace_id:
            conds.append("trace_id = %(tid)s")
            params["tid"] = trace_id
        if run_id:
            conds.append("run_id = toUUID(%(rid)s)")
            params["rid"] = run_id
        if metric:
            conds.append("metric = %(metric)s")
            params["metric"] = metric
        where = ("WHERE " + " AND ".join(conds)) if conds else "WHERE evaluated_at >= now() - INTERVAL 720 HOUR"
        return self._run(
            f"SELECT toString(id) AS id, trace_id, toString(run_id) AS run_id, "
            f"metric, score, reasoning, evaluator, eval_type, evaluated_at "
            f"FROM otel.eval_scores "
            f"{where} "
            f"ORDER BY evaluated_at DESC LIMIT %(limit)s",
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


def _er(path: str, params: dict | None = None) -> Any:
    try:
        r = httpx.get(f"{EVAL_RUNNER_URL}{path}", params=params or {}, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


def _er_post(path: str, body: dict) -> Any:
    try:
        r = httpx.post(f"{EVAL_RUNNER_URL}{path}", json=body, timeout=60)
        r.raise_for_status()
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


# ── Tool implementations ───────────────────────────────────────────────────────

def get_recent_traces(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    hours = inputs.get("hours", 720)
    limit = min(int(inputs.get("limit", 20)), 50)
    try:
        rows = get_db().get_recent_traces(agent_role, hours, limit)
        note = "Only agent.task spans are returned (spans with agent.role populated). Auto-expands to 30d if shorter window is empty."
        return json.dumps({"traces": rows, "count": len(rows), "window_hours": hours, "note": note}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_agent_performance(inputs: dict) -> str:
    hours = inputs.get("hours", 720)
    try:
        scores = get_db().get_eval_scores(hours)
        return json.dumps({"eval_scores_by_metric": scores, "hours": hours, "count": len(scores)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_cost_breakdown(inputs: dict) -> str:
    hours = inputs.get("hours", 720)
    source = inputs.get("source", "")
    try:
        rows = get_db().get_cost_breakdown(hours, source=source)
        total = sum(float(r.get("total_cost_usd", 0)) for r in rows)
        total_in = sum(int(r.get("total_input_tokens", 0)) for r in rows)
        total_out = sum(int(r.get("total_output_tokens", 0)) for r in rows)
        return json.dumps({
            "by_agent": rows,
            "totals": {
                "cost_usd": round(total, 6),
                "input_tokens": total_in,
                "output_tokens": total_out,
            },
            "window_hours": hours,
            "source_filter": source or "all",
            "note": "Data from otel.prompt_evals — same source as Prompt Lab. Rates from gov_threshold_config.",
        }, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_error_rates(inputs: dict) -> str:
    hours = inputs.get("hours", 168)
    try:
        rows = get_db().get_error_rate(hours)
        return json.dumps({"by_agent": rows, "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_safety_events(inputs: dict) -> str:
    hours = inputs.get("hours", 24)
    agent_role = inputs.get("agent_role", "")
    params: dict = {"hours": hours, "limit": 100}
    if agent_role:
        params["agent_role"] = agent_role
    data = _gov("/safety/events", params) or []
    return json.dumps(data, default=str)


def get_thresholds(inputs: dict) -> str:
    category = inputs.get("category", "")
    data = _gov("/thresholds") or []
    if category and isinstance(data, list):
        data = [t for t in data if t.get("category") == category]
    return json.dumps(data, default=str)


def get_agent_budgets(inputs: dict) -> str:
    agent_role = inputs.get("agent_role", "")
    if agent_role:
        data = _gov(f"/budget/{agent_role}")
    else:
        data = _gov("/budget")
    return json.dumps(data or {}, default=str)


def get_version_pins(_: dict) -> str:
    data = _gov("/lifecycle/versions") or []
    return json.dumps(data, default=str)


def get_lifecycle_changes(inputs: dict) -> str:
    hours = inputs.get("hours", 168)
    agent_role = inputs.get("agent_role", "")
    params: dict = {"hours": hours, "limit": 100}
    if agent_role:
        params["agent_role"] = agent_role
    data = _gov("/lifecycle/changes", params) or []
    return json.dumps(data, default=str)


def get_model_registry(_: dict) -> str:
    data = _gov("/model-registry") or []
    return json.dumps(data, default=str)


def get_compliance_scorecard(inputs: dict) -> str:
    framework = inputs.get("framework", "")
    params = {"framework": framework} if framework else {}
    data = _gov("/regulatory/scorecard", params) or {}
    return json.dumps(data, default=str)


def get_risk_register(inputs: dict) -> str:
    status = inputs.get("status", "")
    params = {"status": status} if status else {}
    data = _gov("/regulatory/risk-register", params) or []
    return json.dumps(data, default=str)


def get_prompt_detail(inputs: dict) -> str:
    trace_id = inputs.get("trace_id", "")
    if not trace_id:
        return json.dumps({"error": "trace_id is required"})
    try:
        rows = get_db().get_prompt_detail(trace_id)
        return json.dumps({"prompts": rows, "count": len(rows)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def search_prompts(inputs: dict) -> str:
    agent_name = inputs.get("agent_name", "")
    source = inputs.get("source", "")
    conversation_id = inputs.get("conversation_id", "")
    hours = inputs.get("hours", 720)
    limit = inputs.get("limit", 10)
    try:
        rows = get_db().search_prompts(agent_name, source, conversation_id, hours, limit)
        return json.dumps({"prompts": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_eval_runs(inputs: dict) -> str:
    suite = inputs.get("suite", "")
    hours = inputs.get("hours", 720)
    limit = inputs.get("limit", 20)
    try:
        rows = get_db().get_eval_runs(suite, hours, limit)
        return json.dumps({"runs": rows, "count": len(rows), "window_hours": hours}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_benchmarks(inputs: dict) -> str:
    suite = inputs.get("suite", "")
    difficulty = inputs.get("difficulty", "")
    limit = inputs.get("limit", 50)
    try:
        rows = get_db().get_benchmarks(suite, difficulty, limit)
        return json.dumps({"benchmarks": rows, "count": len(rows)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def get_eval_scores_detail(inputs: dict) -> str:
    trace_id = inputs.get("trace_id", "")
    run_id = inputs.get("run_id", "")
    metric = inputs.get("metric", "")
    limit = inputs.get("limit", 20)
    try:
        rows = get_db().get_eval_scores_detail(trace_id, run_id, metric, limit)
        return json.dumps({"scores": rows, "count": len(rows)}, default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


def list_benchmarks(inputs: dict) -> str:
    suite = inputs.get("suite", "")
    limit = int(inputs.get("limit", 50))
    data = _er("/benchmarks", {"suite": suite, "limit": limit})
    return json.dumps(data, default=str)


def create_benchmark(inputs: dict) -> str:
    body = {
        "name":             inputs.get("name", "").strip(),
        "task_input":       inputs.get("task_input", "").strip(),
        "suite":            inputs.get("suite", "unit"),
        "difficulty":       inputs.get("difficulty", "medium"),
        "expected_output":  inputs.get("expected_output", ""),
        "rubric":           inputs.get("rubric", ""),
        "tags":             inputs.get("tags", ""),
        "dataset_version":  inputs.get("dataset_version", "v1"),
    }
    if not body["name"] or not body["task_input"]:
        return json.dumps({"error": "name and task_input are required"})
    return json.dumps(_er_post("/benchmarks", body), default=str)


def create_eval_run(inputs: dict) -> str:
    body = {
        "name":          inputs.get("name", "").strip(),
        "suite":         inputs.get("suite", "unit"),
        "agent_version": inputs.get("agent_version", "v1"),
        "metadata":      inputs.get("metadata", {}),
    }
    if not body["name"]:
        return json.dumps({"error": "name is required"})
    return json.dumps(_er_post("/runs", body), default=str)


def execute_benchmark_run(inputs: dict) -> str:
    run_id = inputs.get("run_id", "")
    agent_endpoint = inputs.get("agent_endpoint", "")
    benchmark_ids = inputs.get("benchmark_ids", [])
    if not run_id or not agent_endpoint or not benchmark_ids:
        return json.dumps({"error": "run_id, agent_endpoint, and benchmark_ids are required"})
    body = {"agent_endpoint": agent_endpoint, "benchmark_ids": benchmark_ids}
    return json.dumps(_er_post(f"/runs/{run_id}/execute", body), default=str)


def trigger_run_evaluation(inputs: dict) -> str:
    run_id = inputs.get("run_id", "")
    if not run_id:
        return json.dumps({"error": "run_id is required"})
    return json.dumps(_er_post(f"/runs/{run_id}/evaluate", {"mode": "offline"}), default=str)


def set_baseline_run(inputs: dict) -> str:
    run_id = inputs.get("run_id", "")
    if not run_id:
        return json.dumps({"error": "run_id is required"})
    try:
        r = httpx.put(f"{EVAL_RUNNER_URL}/runs/{run_id}/baseline", timeout=10)
        r.raise_for_status()
        return json.dumps(r.json(), default=str)
    except Exception as exc:
        return json.dumps({"error": str(exc)})


# ── Tool dispatcher ────────────────────────────────────────────────────────────

TOOL_MAP = {
    "get_recent_traces":       get_recent_traces,
    "get_agent_performance":   get_agent_performance,
    "get_cost_breakdown":      get_cost_breakdown,
    "get_error_rates":         get_error_rates,
    "get_safety_events":       get_safety_events,
    "get_thresholds":          get_thresholds,
    "get_agent_budgets":       get_agent_budgets,
    "get_version_pins":        get_version_pins,
    "get_lifecycle_changes":   get_lifecycle_changes,
    "get_model_registry":      get_model_registry,
    "get_compliance_scorecard": get_compliance_scorecard,
    "get_risk_register":       get_risk_register,
    "get_prompt_detail":       get_prompt_detail,
    "search_prompts":          search_prompts,
    "get_eval_runs":           get_eval_runs,
    "get_benchmarks":          get_benchmarks,
    "get_eval_scores_detail":  get_eval_scores_detail,
    "list_benchmarks":         list_benchmarks,
    "create_benchmark":        create_benchmark,
    "create_eval_run":         create_eval_run,
    "execute_benchmark_run":   execute_benchmark_run,
    "trigger_run_evaluation":  trigger_run_evaluation,
    "set_baseline_run":        set_baseline_run,
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
        "name": "get_recent_traces",
        "description": "Get recent agent task spans from OTel traces. Returns agent.task spans with agent_role, duration, status, and timestamp. Default window is 30 days — auto-expands if shorter window returns empty.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Filter by agent role. Empty = all agents."},
                "hours": {"type": "integer", "description": "Look-back window in hours. Default: 720 (30 days)."},
                "limit": {"type": "integer", "description": "Max traces to return (max 50, default 20)"},
            },
        },
    },
    {
        "name": "get_agent_performance",
        "description": "Get eval scores by metric (faithfulness, hallucination, relevance, coherence, etc.) aggregated over the given window. Default 30 days.",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"}},
        },
    },
    {
        "name": "get_cost_breakdown",
        "description": "Get token usage and USD cost broken down by agent from otel.prompt_evals. Rates from governance config. Filter by source for production vs benchmark costs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"},
                "source": {
                    "type": "string",
                    "enum": ["production", "benchmark", "exploratory", ""],
                    "description": "Filter by trace source. '' = all combined.",
                },
            },
        },
    },
    {
        "name": "get_error_rates",
        "description": "Get error rates per agent role on agent.task spans over the given time window. Default 7 days.",
        "input_schema": {
            "type": "object",
            "properties": {"hours": {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"}},
        },
    },
    {
        "name": "get_safety_events",
        "description": "Get safety rule violation events including the matched pattern and matched_text that triggered the violation.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 24)"},
                "agent_role": {"type": "string", "description": "Filter by agent role"},
            },
        },
    },
    {
        "name": "get_thresholds",
        "description": "Get all configurable governance thresholds (token budgets, cost limits, SLO targets, safety scores, etc.) with current values and units.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {"type": "string", "description": "Filter by category (e.g. 'budget', 'slo', 'safety', 'trust')"},
            },
        },
    },
    {
        "name": "get_agent_budgets",
        "description": "Get token budget configuration and current daily usage per agent. Shows daily_token_limit, cost_usd_limit, tokens used today, and budget utilization percentage.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_role": {"type": "string", "description": "Specific agent to look up. Empty = all agents."},
            },
        },
    },
    {
        "name": "get_version_pins",
        "description": "Get model version pin configuration per agent — which model version is pinned, deployment mode, and whether the pin is active.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_lifecycle_changes",
        "description": "Get the configuration change log — what was changed, old vs new values, who changed it, and when.",
        "input_schema": {
            "type": "object",
            "properties": {
                "hours": {"type": "integer", "description": "Look-back window in hours (default 168 = 7 days)"},
                "agent_role": {"type": "string", "description": "Filter by agent"},
            },
        },
    },
    {
        "name": "get_model_registry",
        "description": "Get the approved model registry — which models are whitelisted, their provider, license type, commercial use flag, and sector restrictions.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_compliance_scorecard",
        "description": "Get compliance scorecard scores by regulatory framework (SOC2, GDPR, HIPAA, etc.) showing controls passing/failing per agent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "framework": {"type": "string", "description": "Filter by framework name (e.g. 'SOC2', 'GDPR'). Empty = all."},
            },
        },
    },
    {
        "name": "get_risk_register",
        "description": "Get the AI governance risk register — all tracked risks with likelihood, impact, owner, mitigation status, and category.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "enum": ["open", "mitigated", "accepted", "closed", ""], "description": "Filter by risk status"},
            },
        },
    },
    {
        "name": "get_prompt_detail",
        "description": "Get the full prompt text, response text, token counts, cost_usd, eval scores, and conversation_id for a specific trace ID.",
        "input_schema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "The trace ID to look up in otel.prompt_evals"},
            },
            "required": ["trace_id"],
        },
    },
    {
        "name": "search_prompts",
        "description": "Search recent prompt/response pairs from otel.prompt_evals. Returns prompt_text, response_text, agent, model, tokens, scores, source, and conversation_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_name": {"type": "string", "description": "Filter by agent name"},
                "source": {
                    "type": "string",
                    "enum": ["production", "benchmark", "exploratory", ""],
                    "description": "Filter by source. Empty = all.",
                },
                "conversation_id": {"type": "string", "description": "Filter by conversation/session ID"},
                "hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"},
                "limit": {"type": "integer", "description": "Max results (default 10, max 50)"},
            },
        },
    },
    {
        "name": "get_eval_runs",
        "description": "List evaluation test runs from otel.eval_runs. Shows run name, suite, agent version, baseline flag, and timestamp.",
        "input_schema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": ["unit", "integration", "collaboration", "production", ""], "description": "Filter by test suite"},
                "hours": {"type": "integer", "description": "Look-back window in hours (default 720 = 30 days)"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    },
    {
        "name": "get_benchmarks",
        "description": "List benchmark test case definitions including task_input, expected_output, rubric, suite, and difficulty.",
        "input_schema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": ["unit", "integration", "collaboration", "production", ""], "description": "Filter by suite"},
                "difficulty": {"type": "string", "enum": ["easy", "medium", "hard", ""], "description": "Filter by difficulty"},
                "limit": {"type": "integer", "description": "Max results (default 50)"},
            },
        },
    },
    {
        "name": "get_eval_scores_detail",
        "description": "Get eval scores with full reasoning text for a specific trace or run. Shows metric, score, and the evaluator's justification.",
        "input_schema": {
            "type": "object",
            "properties": {
                "trace_id": {"type": "string", "description": "Filter by specific trace"},
                "run_id": {"type": "string", "description": "Filter by eval run"},
                "metric": {"type": "string", "description": "Filter by metric name"},
                "limit": {"type": "integer", "description": "Max results (default 20)"},
            },
        },
    },
    {
        "name": "list_benchmarks",
        "description": "List benchmark test cases from the eval-runner, optionally filtered by suite.",
        "input_schema": {
            "type": "object",
            "properties": {
                "suite": {"type": "string", "enum": ["unit", "integration", "collaboration", "production", ""], "description": "Filter by suite. Empty = all."},
                "limit": {"type": "integer", "description": "Max results. Default 50."},
            },
        },
    },
    {
        "name": "create_benchmark",
        "description": "Create a new benchmark test case in the eval system. Use to build test suites for evaluating agent quality.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":            {"type": "string", "description": "REQUIRED. Short descriptive name for the test."},
                "task_input":      {"type": "string", "description": "REQUIRED. The prompt/question the agent will receive."},
                "suite":           {"type": "string", "enum": ["unit", "integration", "collaboration", "production"], "description": "Test suite category. Default 'unit'."},
                "difficulty":      {"type": "string", "enum": ["easy", "medium", "hard"], "description": "Difficulty level. Default 'medium'."},
                "expected_output": {"type": "string", "description": "Reference answer for scoring."},
                "rubric":          {"type": "string", "description": "JSON rubric string for the evaluator."},
                "tags":            {"type": "string", "description": "Comma-separated tags."},
                "dataset_version": {"type": "string", "description": "Dataset version label. Default 'v1'."},
            },
            "required": ["name", "task_input"],
        },
    },
    {
        "name": "create_eval_run",
        "description": "Create a new eval run to group benchmark executions. Returns a run_id needed for execute_benchmark_run.",
        "input_schema": {
            "type": "object",
            "properties": {
                "name":          {"type": "string", "description": "REQUIRED. Human-readable run name."},
                "suite":         {"type": "string", "enum": ["unit", "integration", "collaboration", "production"], "description": "Suite being tested. Default 'unit'."},
                "agent_version": {"type": "string", "description": "Agent version label. Default 'v1'."},
                "metadata":      {"type": "object", "description": "Arbitrary key-value metadata."},
            },
            "required": ["name"],
        },
    },
    {
        "name": "execute_benchmark_run",
        "description": "Send benchmark tasks to an agent endpoint and collect results. Eval scores appear automatically in ~30s per task via OTel spans.",
        "input_schema": {
            "type": "object",
            "properties": {
                "run_id":          {"type": "string", "description": "REQUIRED. run_id from create_eval_run()."},
                "agent_endpoint":  {"type": "string", "description": "REQUIRED. Full URL of the agent's chat endpoint."},
                "benchmark_ids":   {"type": "array", "items": {"type": "string"}, "description": "REQUIRED. List of benchmark_id UUIDs to run."},
            },
            "required": ["run_id", "agent_endpoint", "benchmark_ids"],
        },
    },
    {
        "name": "trigger_run_evaluation",
        "description": "Trigger offline re-evaluation of all traces in a run. Useful after changing evaluator config or scoring rubrics.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "REQUIRED. The run_id to re-evaluate."}},
            "required": ["run_id"],
        },
    },
    {
        "name": "set_baseline_run",
        "description": "Promote an eval run to the regression baseline. Future runs will be compared against this baseline.",
        "input_schema": {
            "type": "object",
            "properties": {"run_id": {"type": "string", "description": "REQUIRED. The run_id to set as baseline."}},
            "required": ["run_id"],
        },
    },
]
