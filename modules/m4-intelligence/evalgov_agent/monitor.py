"""Proactive monitor — polls all system signals every 60s, generates RCA findings via Claude.

Signal sources:
  - Governance (M2): circuit breakers, HITL, rogue agents, incidents, trust scores,
                     safety violations, policy blocks, statistical anomalies, behavior violations
  - Gateway (M3):    routing error rates, unknown/misconfigured roles, shadow model wins,
                     stale A/B tests
  - Quality & Cost:  correctness regressions, cost spikes
"""

import asyncio
import json
import os
from datetime import datetime, timezone

import httpx
import structlog

from db import AgentDB

log = structlog.get_logger()

GOV_URL           = os.getenv("GOVERNANCE_SERVICE_URL", "http://localhost:8002")
POLL_INTERVAL     = int(os.getenv("MONITOR_POLL_SECONDS", "60"))
HITL_TIMEOUT_MINUTES = int(os.getenv("HITL_TIMEOUT_MINUTES", "15"))


def _gov(path: str, params: dict | None = None) -> list | dict:
    try:
        r = httpx.get(f"{GOV_URL}{path}", params=params or {}, timeout=8)
        r.raise_for_status()
        return r.json()
    except Exception:
        return []


class ProactiveMonitor:
    def __init__(self, db: AgentDB):
        self.db = db
        self._task: asyncio.Task | None = None

    def start(self):
        self._task = asyncio.create_task(self._loop())
        log.info("monitor_started", poll_interval_s=POLL_INTERVAL)

    def stop(self):
        if self._task:
            self._task.cancel()

    def run_once(self) -> dict:
        """Run all checks immediately and return per-source summary. Used by /monitor/run."""
        results = {}
        for source, fn in [
            ("governance", self._detect_gov_anomalies),
            ("gateway",    self._detect_gateway_anomalies),
            ("quality",    self._detect_quality_anomalies),
        ]:
            try:
                anomalies = fn()
                created   = self._process_anomalies(anomalies)
                results[source] = {"detected": len(anomalies), "new_findings": created}
            except Exception as exc:
                results[source] = {"error": str(exc)}
        return results

    async def _loop(self):
        await asyncio.sleep(10)
        while True:
            try:
                await self._run_cycle()
            except Exception as exc:
                log.error("monitor_cycle_error", error=str(exc))
            await asyncio.sleep(POLL_INTERVAL)

    async def _run_cycle(self):
        loop = asyncio.get_event_loop()
        anomalies = await loop.run_in_executor(None, self._detect_anomalies)
        await loop.run_in_executor(None, self._process_anomalies, anomalies)

    def _detect_anomalies(self) -> list[dict]:
        anomalies: list[dict] = []
        anomalies.extend(self._detect_gov_anomalies())
        anomalies.extend(self._detect_gateway_anomalies())
        anomalies.extend(self._detect_quality_anomalies())
        return anomalies

    def _process_anomalies(self, anomalies: list[dict]) -> int:
        """Save new anomalies as findings with RCA. Returns count of new findings created."""
        created = 0
        for anomaly in anomalies:
            atype = anomaly["type"]
            agent = anomaly.get("agent", "system")
            if self.db.finding_exists_recently(atype, agent):
                continue
            rca_data = self._generate_rca(anomaly)
            self.db.insert_finding(
                finding_type=atype,
                severity=rca_data.get("severity", anomaly.get("default_severity", "medium")),
                title=anomaly["title"],
                summary=rca_data.get("summary", anomaly.get("summary", "")),
                rca=rca_data.get("rca", ""),
                recommendation=rca_data.get("recommendation", ""),
                signal_data=json.dumps(anomaly.get("signal", {}), default=str),
                affected_agent=agent,
            )
            log.info("finding_created", type=atype, agent=agent, severity=rca_data.get("severity"))
            created += 1
        return created

    # ── Governance signals (M2) ───────────────────────────────────────────────

    def _detect_gov_anomalies(self) -> list[dict]:
        anomalies: list[dict] = []

        # 1. Open circuit breakers
        cbs = _gov("/enforcement/circuit-breakers") or []
        for cb in cbs if isinstance(cbs, list) else []:
            if cb.get("state", "").upper() == "OPEN":
                anomalies.append({
                    "type": "circuit_breaker_open",
                    "agent": cb.get("agent_role", "unknown"),
                    "title": f"Circuit Breaker OPEN: {cb.get('agent_role', 'unknown')}",
                    "summary": f"Agent {cb.get('agent_role')} circuit breaker is OPEN — all actions are blocked.",
                    "default_severity": "critical",
                    "signal": {
                        "state": cb.get("state"),
                        "failure_count": cb.get("failure_count"),
                        "opened_at": str(cb.get("opened_at", "")),
                        "quarantine_reason": cb.get("quarantine_reason", ""),
                    },
                })

        # 2. HITL requests — quality gate entries shown immediately;
        #    all other pending entries shown after HITL_TIMEOUT_MINUTES
        hitl = _gov("/hitl/queue?status=pending&limit=50") or []
        now = datetime.now(timezone.utc)
        for req in hitl if isinstance(hitl, list) else []:
            created_raw = str(req.get("created_at", ""))
            try:
                created_dt = datetime.fromisoformat(created_raw.replace("Z", "+00:00"))
                if not created_dt.tzinfo:
                    created_dt = created_dt.replace(tzinfo=timezone.utc)
                wait_minutes = (now - created_dt).total_seconds() / 60
            except Exception:
                continue

            action_type = str(req.get("action_type", ""))
            payload = req.get("payload", "{}")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except Exception:
                    payload = {}
            agent = payload.get("agent_role", req.get("run_id", "unknown"))
            ctx   = payload.get("context", {}) if isinstance(payload.get("context"), dict) else {}

            if action_type == "quality_gate_hold":
                metric    = ctx.get("metric", "unknown")
                score     = ctx.get("score", 0)
                threshold = ctx.get("threshold", 0)
                query     = ctx.get("query", "")
                rca_hint  = (
                    f"{metric} scored {score:.3f} against threshold {threshold}. "
                    + (f'Prompt: "{query[:120]}"' if query else "No prompt context available.")
                )
                anomalies.append({
                    "type": "quality_gate_hold_pending",
                    "agent": agent,
                    "title": f"Quality Gate Hold Awaiting Review: {agent} [{metric}]",
                    "summary": (
                        f"Agent {agent} has a quality gate hold pending review "
                        f"({int(wait_minutes)}m ago). {rca_hint} "
                        f"Approve to forgive (no CB impact). Reject to confirm failure "
                        f"(counts toward block threshold). Auto-expires in "
                        f"{max(0, 5 - int(wait_minutes))}m."
                    ),
                    "default_severity": "high",
                    "signal": {
                        "request_id":  req.get("request_id"),
                        "metric":      metric,
                        "score":       score,
                        "threshold":   threshold,
                        "wait_minutes": round(wait_minutes, 1),
                        "query":       query[:200] if query else "",
                    },
                })
            elif action_type == "quality_gate_block":
                metric    = ctx.get("metric", "unknown")
                score     = ctx.get("score", 0)
                threshold = ctx.get("threshold", 0)
                anomalies.append({
                    "type": "quality_gate_block_pending",
                    "agent": agent,
                    "title": f"Quality Gate Block — CB Failure Recorded: {agent} [{metric}]",
                    "summary": (
                        f"Agent {agent} quality gate block fired ({int(wait_minutes)}m ago). "
                        f"{metric} scored {score:.3f} (threshold {threshold}). "
                        f"CB failure has been recorded. "
                        f"Approve to override and decrement CB failure count. "
                        f"Reject to confirm — CB failure stands."
                    ),
                    "default_severity": "critical",
                    "signal": {
                        "request_id": req.get("request_id"),
                        "metric":     metric,
                        "score":      score,
                        "threshold":  threshold,
                        "wait_minutes": round(wait_minutes, 1),
                    },
                })
            elif wait_minutes >= HITL_TIMEOUT_MINUTES:
                anomalies.append({
                    "type": "hitl_timeout",
                    "agent": agent,
                    "title": f"HITL Request Waiting {int(wait_minutes)}m: {agent}",
                    "summary": (
                        f"Agent {agent} is blocked waiting for HITL approval for "
                        f"{int(wait_minutes)} minutes (threshold: {HITL_TIMEOUT_MINUTES}m)."
                    ),
                    "default_severity": "high",
                    "signal": {
                        "request_id":  req.get("request_id"),
                        "risk_tier":   req.get("risk_tier"),
                        "action_type": action_type,
                        "wait_minutes": round(wait_minutes, 1),
                    },
                })

        # 3. Rogue agents with quarantine recommended
        rogue = _gov("/enforcement/rogue-assessments") or []
        for r in rogue if isinstance(rogue, list) else []:
            if r.get("quarantine_recommended"):
                agent = r.get("agent_role", "unknown")
                anomalies.append({
                    "type": "rogue_agent_detected",
                    "agent": agent,
                    "title": f"Rogue Agent Detected: {agent}",
                    "summary": f"Agent {agent} has anomalous behavior patterns — quarantine is recommended.",
                    "default_severity": "critical",
                    "signal": {
                        "composite_score": r.get("composite_score"),
                        "frequency_score": r.get("frequency_score"),
                        "entropy_score": r.get("entropy_score"),
                        "capability_score": r.get("capability_score"),
                    },
                })

        # 4. P0/P1 open incidents
        incidents = _gov("/incidents?status=open&limit=20") or []
        if isinstance(incidents, list):
            for inc in incidents:
                sev = inc.get("severity", "p2")
                if sev in ("p0", "p1"):
                    agent = inc.get("agent_role", "system")
                    anomalies.append({
                        "type": "critical_incident",
                        "agent": agent,
                        "title": f"Open Incident [{sev.upper()}]: {inc.get('incident_type', 'unknown')} — {agent}",
                        "summary": f"A {sev} severity incident of type '{inc.get('incident_type')}' is open for agent {agent}.",
                        "default_severity": "critical" if sev == "p0" else "high",
                        "signal": {
                            "incident_id": inc.get("incident_id"),
                            "incident_type": inc.get("incident_type"),
                            "opened_at": str(inc.get("opened_at", "")),
                            "detail": inc.get("detail", "")[:200],
                        },
                    })

        # 5. Low trust scores (below 0.4)
        trust = _gov("/enforcement/trust-scores") or []
        for t in trust if isinstance(trust, list) else []:
            score = float(t.get("trust_score", 1.0))
            if score < 0.4:
                agent = t.get("agent_role", "unknown")
                anomalies.append({
                    "type": "low_trust_score",
                    "agent": agent,
                    "title": f"Low Trust Score: {agent} ({score:.2f})",
                    "summary": f"Agent {agent} has a trust score of {score:.2f} (threshold: 0.4). Indicates identity, behavior, or compliance issues.",
                    "default_severity": "high",
                    "signal": {
                        "trust_score": score,
                        "trust_tier": t.get("trust_tier"),
                        "identity_score": t.get("identity_score"),
                        "behavior_score": t.get("behavior_score"),
                        "compliance_score": t.get("compliance_score"),
                    },
                })

        # 6. Safety violations in last hour (group by agent)
        safety = _gov("/safety/events", {"hours": 1, "limit": 100}) or []
        safety_by_agent: dict = {}
        for ev in safety if isinstance(safety, list) else []:
            if not ev.get("detected"):
                continue
            agent = ev.get("agent_role", "system")
            safety_by_agent.setdefault(agent, []).append(ev)
        for agent, evts in safety_by_agent.items():
            types = list({e.get("event_type", "unknown") for e in evts})
            anomalies.append({
                "type": "safety_violation",
                "agent": agent,
                "title": f"Safety Violation: {agent} — {', '.join(types)} ({len(evts)} event{'s' if len(evts) > 1 else ''})",
                "summary": f"Agent {agent} triggered {len(evts)} safety detection(s) in the last hour: {', '.join(types)}.",
                "default_severity": "high",
                "signal": {
                    "event_count": len(evts),
                    "event_types": types,
                    "patterns": list({e.get("pattern_name", "") for e in evts if e.get("pattern_name")}),
                    "sample_detail": evts[0].get("detail", "")[:200] if evts else "",
                },
            })

        # 7. Policy hard blocks in last hour
        try:
            policy_blocks = self.db._run(
                "SELECT agent_role, count() AS cnt, groupArray(metric)[1] AS sample_metric, "
                "groupArray(message)[1] AS sample_message "
                "FROM otel.gov_policy_decisions "
                "WHERE decision = 'block' AND ts >= now() - INTERVAL 1 HOUR "
                "GROUP BY agent_role ORDER BY cnt DESC LIMIT 20"
            )
            for row in policy_blocks or []:
                agent = row.get("agent_role", "system")
                anomalies.append({
                    "type": "policy_block",
                    "agent": agent,
                    "title": f"Policy Hard Block: {agent} ({int(row.get('cnt', 1))} block{'s' if int(row.get('cnt', 1)) > 1 else ''} in last hour)",
                    "summary": f"Agent {agent} was hard-blocked {row.get('cnt')} time(s) in the last hour. Metric: {row.get('sample_metric', 'unknown')}.",
                    "default_severity": "high",
                    "signal": {
                        "block_count": row.get("cnt"),
                        "sample_metric": row.get("sample_metric"),
                        "sample_message": row.get("sample_message", "")[:200],
                    },
                })
        except Exception as exc:
            log.warning("monitor_policy_blocks_failed", error=str(exc))

        # 8. Statistical anomalies — z-score >= 3 in last hour
        stat_anomalies = _gov("/anomalies", {"hours": 1, "limit": 100}) or []
        stat_by_agent: dict = {}
        for ev in stat_anomalies if isinstance(stat_anomalies, list) else []:
            if float(ev.get("z_score", 0) or 0) < 3.0:
                continue
            agent = ev.get("agent_role", "system")
            stat_by_agent.setdefault(agent, []).append(ev)
        for agent, evts in stat_by_agent.items():
            metrics = list({e.get("metric", "unknown") for e in evts})
            worst = max(evts, key=lambda e: float(e.get("z_score", 0) or 0))
            anomalies.append({
                "type": "metric_anomaly",
                "agent": agent,
                "title": f"Metric Anomaly: {agent} — {', '.join(metrics[:3])} (z={float(worst.get('z_score', 0)):.1f})",
                "summary": f"Agent {agent} has {len(evts)} metric(s) with statistically significant deviations (z≥3) in the last hour: {', '.join(metrics[:3])}.",
                "default_severity": "medium",
                "signal": {
                    "anomaly_count": len(evts),
                    "metrics": metrics,
                    "worst_metric": worst.get("metric"),
                    "worst_z_score": float(worst.get("z_score", 0) or 0),
                    "worst_observed": worst.get("observed_value"),
                    "worst_baseline": worst.get("baseline_mean"),
                },
            })

        # 9. Behavior scope violations in last hour
        behavior = _gov("/behavior/events", {"hours": 1, "limit": 100}) or []
        behavior_by_agent: dict = {}
        for ev in behavior if isinstance(behavior, list) else []:
            if ev.get("event_type") not in ("scope_violation", "policy_violation", "capability_abuse"):
                continue
            agent = ev.get("agent_role", "system")
            behavior_by_agent.setdefault(agent, []).append(ev)
        for agent, evts in behavior_by_agent.items():
            types = list({e.get("event_type", "unknown") for e in evts})
            anomalies.append({
                "type": "behavior_violation",
                "agent": agent,
                "title": f"Behavior Violation: {agent} — {', '.join(types)} ({len(evts)} event{'s' if len(evts) > 1 else ''})",
                "summary": f"Agent {agent} had {len(evts)} behavior violation(s) in the last hour: {', '.join(types)}.",
                "default_severity": "high",
                "signal": {
                    "event_count": len(evts),
                    "event_types": types,
                    "sample_detail": evts[0].get("detail", "")[:200] if evts else "",
                },
            })

        return anomalies

    # ── Gateway signals (M3) ──────────────────────────────────────────────────

    def _detect_gateway_anomalies(self) -> list[dict]:
        anomalies: list[dict] = []

        # 1. High error rate on agents passing through the gateway
        try:
            rows = self.db._run("""
                SELECT agent_role,
                       count() AS total_calls,
                       countIf(status = 'error') AS error_calls,
                       round(countIf(status = 'error') / count(), 3) AS error_rate,
                       any(model_used) AS model_used
                FROM otel.gateway_call_log
                WHERE is_shadow = 0 AND created_at >= now() - INTERVAL 1 HOUR
                  AND agent_role != '' AND agent_role != 'unknown'
                GROUP BY agent_role
                HAVING total_calls >= 5 AND error_rate > 0.20
            """)
            for row in rows:
                agent      = row.get("agent_role", "unknown")
                error_rate = float(row.get("error_rate", 0))
                total      = int(row.get("total_calls", 0))
                model      = row.get("model_used", "unknown")
                anomalies.append({
                    "type": "gateway_routing_error_rate",
                    "agent": agent,
                    "title": f"Gateway Error Rate: {agent} — {int(error_rate * 100)}% errors on {model}",
                    "summary": (
                        f"Agent {agent} has a {int(error_rate * 100)}% error rate in the gateway "
                        f"over the last hour ({int(row.get('error_calls', 0))}/{total} calls on model {model}). "
                        f"This may indicate a misconfigured routing policy or an unhealthy upstream model."
                    ),
                    "default_severity": "critical" if error_rate > 0.5 else "warning",
                    "signal": {
                        "agent_role":  agent,
                        "error_rate":  error_rate,
                        "error_calls": row.get("error_calls"),
                        "total_calls": total,
                        "model_used":  model,
                    },
                })
        except Exception as exc:
            log.warning("monitor_gateway_error_rate_failed", error=str(exc))

        # 2. Unknown/misconfigured role flooding gateway
        try:
            rows = self.db._run("""
                SELECT count() AS total,
                       countIf(status = 'error') AS errors,
                       round(countIf(status = 'error') / count(), 3) AS error_rate
                FROM otel.gateway_call_log
                WHERE agent_role = 'unknown' AND created_at >= now() - INTERVAL 1 HOUR
            """)
            if rows:
                total      = int(rows[0].get("total", 0))
                errors     = int(rows[0].get("errors", 0))
                error_rate = float(rows[0].get("error_rate", 0))
                if total >= 10 and error_rate > 0.50:
                    anomalies.append({
                        "type": "gateway_unknown_role",
                        "agent": "unknown",
                        "title": f"Unknown Agent Role: {total} calls, {int(error_rate * 100)}% errors in last hour",
                        "summary": (
                            f"The gateway received {total} calls from an 'unknown' agent role in the last hour "
                            f"with {int(error_rate * 100)}% error rate. "
                            f"This typically indicates a misconfigured agent (missing agent.role attribute) "
                            f"or an invalid/unregistered API key flooding the gateway."
                        ),
                        "default_severity": "high",
                        "signal": {
                            "total_calls": total,
                            "error_calls": errors,
                            "error_rate":  error_rate,
                        },
                    })
        except Exception as exc:
            log.warning("monitor_gateway_unknown_role_failed", error=str(exc))

        # 3. Shadow model consistently outperforming primary (promote opportunity)
        try:
            shadow_rows = self.db._run("""
                SELECT agent_role, model_used,
                       round(avgIf(scores['faithfulness'], scores['faithfulness'] > 0), 3) AS avg_faithfulness,
                       count() AS scored_count
                FROM otel.gateway_shadow_evals
                WHERE scored_at >= now() - INTERVAL 24 HOUR
                GROUP BY agent_role, model_used
                HAVING scored_count >= 20 AND avg_faithfulness > 0
            """)
            primary_rows = self.db._run("""
                SELECT pe.agent_name AS agent_role,
                       gw.model_used,
                       round(avgIf(pe.scores['faithfulness'], pe.scores['faithfulness'] > 0), 3) AS avg_faithfulness,
                       count() AS eval_count
                FROM otel.prompt_evals pe
                LEFT JOIN (
                    SELECT trace_id, model_used FROM otel.gateway_call_log
                    WHERE is_shadow = 0 AND trace_id != '' AND created_at >= now() - INTERVAL 24 HOUR
                ) gw ON pe.trace_id = gw.trace_id
                WHERE pe.created_at >= now() - INTERVAL 24 HOUR AND gw.model_used != ''
                GROUP BY pe.agent_name, gw.model_used
                HAVING eval_count >= 20 AND avg_faithfulness > 0
            """)
            primary_by_agent: dict[str, float] = {}
            for r in primary_rows:
                agent = r["agent_role"]
                faith = float(r.get("avg_faithfulness", 0))
                if faith > primary_by_agent.get(agent, 0):
                    primary_by_agent[agent] = faith

            for sr in shadow_rows:
                agent        = sr["agent_role"]
                shadow_model = sr["model_used"]
                shadow_faith = float(sr.get("avg_faithfulness", 0))
                primary_faith = primary_by_agent.get(agent, 0)
                if primary_faith > 0 and shadow_faith > primary_faith * 1.10:
                    lift = round((shadow_faith - primary_faith) / primary_faith * 100, 1)
                    anomalies.append({
                        "type": "gateway_shadow_winning",
                        "agent": agent,
                        "title": f"Shadow Model Winning: {shadow_model} for {agent} (+{lift}% faithfulness)",
                        "summary": (
                            f"Shadow model {shadow_model} is outperforming the primary for {agent} "
                            f"with {lift}% higher faithfulness ({shadow_faith:.3f} vs {primary_faith:.3f}) "
                            f"over {sr['scored_count']} evaluations in the last 24h. "
                            f"Consider promoting this model or setting up an A/B test to validate at scale."
                        ),
                        "default_severity": "info",
                        "signal": {
                            "agent_role":          agent,
                            "shadow_model":        shadow_model,
                            "shadow_faithfulness": shadow_faith,
                            "primary_faithfulness": primary_faith,
                            "lift_pct":            lift,
                            "shadow_call_count":   int(sr["scored_count"]),
                        },
                    })
        except Exception as exc:
            log.warning("monitor_shadow_winning_failed", error=str(exc))

        # 4. Stale A/B test running > 24h with no conclusion
        try:
            rows = self.db._run("""
                SELECT test_id, name, primary_model, variant_model, created_at
                FROM otel.gateway_ab_tests
                WHERE status = 'running' AND created_at <= now() - INTERVAL 24 HOUR
            """)
            for row in rows:
                test_id = str(row.get("test_id", ""))
                name    = row.get("name", "unknown")
                anomalies.append({
                    "type": "gateway_ab_test_stale",
                    "agent": "gateway",
                    "title": f"Stale A/B Test: '{name}' running > 24 hours",
                    "summary": (
                        f"A/B test '{name}' ({test_id[:8]}) comparing "
                        f"{row.get('primary_model')} vs {row.get('variant_model')} "
                        f"has been running since {str(row.get('created_at'))[:16]} with no conclusion. "
                        f"Review results and either declare a winner or stop the test."
                    ),
                    "default_severity": "info",
                    "signal": {
                        "test_id":       test_id,
                        "test_name":     name,
                        "primary_model": row.get("primary_model"),
                        "variant_model": row.get("variant_model"),
                        "created_at":    str(row.get("created_at")),
                    },
                })
        except Exception as exc:
            log.warning("monitor_ab_test_stale_failed", error=str(exc))

        return anomalies

    # ── Quality & cost signals ────────────────────────────────────────────────

    def _detect_quality_anomalies(self) -> list[dict]:
        anomalies: list[dict] = []

        # 1. Correctness regression: >10% drop in last 2h vs prior 6h baseline
        try:
            rows = self.db._run("""
                SELECT p.agent_name AS agent_name,
                       avgIf(s.score, s.evaluated_at >= now() - INTERVAL 2 HOUR) AS recent_avg,
                       avgIf(s.score, s.evaluated_at >= now() - INTERVAL 8 HOUR
                                      AND s.evaluated_at < now() - INTERVAL 2 HOUR) AS baseline_avg
                FROM otel.eval_scores s
                JOIN otel.prompt_evals p ON s.span_id = p.span_id
                WHERE s.metric = 'correctness' AND s.evaluated_at >= now() - INTERVAL 8 HOUR
                GROUP BY p.agent_name
                HAVING baseline_avg > 0 AND recent_avg > 0
            """)
            for row in rows:
                recent   = float(row.get("recent_avg", 0))
                baseline = float(row.get("baseline_avg", 0))
                agent    = row.get("agent_name", "")
                if baseline > 0 and recent < baseline * 0.90:
                    drop_pct = round((baseline - recent) / baseline * 100, 1)
                    anomalies.append({
                        "type": "quality_regression",
                        "agent": agent,
                        "title": f"Quality Regression: {agent} correctness -{drop_pct}% in last 2h",
                        "summary": (
                            f"{agent} correctness score dropped from {baseline:.3f} to {recent:.3f} "
                            f"({drop_pct}% decline) over the last 2 hours vs the prior 6-hour baseline."
                        ),
                        "default_severity": "critical" if drop_pct >= 20 else "warning",
                        "signal": {
                            "agent_role":   agent,
                            "recent_avg":   recent,
                            "baseline_avg": baseline,
                            "drop_pct":     drop_pct,
                        },
                    })
        except Exception as exc:
            log.warning("monitor_quality_regression_failed", error=str(exc))

        # 2. Cost spike: last hour >2x 6-hour rolling average
        try:
            rows = self.db._run("""
                SELECT
                    sumIf(tokens_in * 0.00000015 + tokens_out * 0.00000060,
                          created_at >= now() - INTERVAL 1 HOUR) AS last_hour_cost,
                    sumIf(tokens_in * 0.00000015 + tokens_out * 0.00000060,
                          created_at >= now() - INTERVAL 7 HOUR
                          AND created_at < now() - INTERVAL 1 HOUR) / 6 AS avg_hourly_cost
                FROM otel.gateway_call_log
                WHERE is_shadow = 0 AND created_at >= now() - INTERVAL 7 HOUR
            """)
            if rows:
                last = float(rows[0].get("last_hour_cost", 0))
                avg  = float(rows[0].get("avg_hourly_cost", 0))
                if avg > 0 and last > avg * 2.0:
                    multiplier = round(last / avg, 1)
                    anomalies.append({
                        "type": "cost_spike",
                        "agent": "system",
                        "title": f"Cost Spike: ${last:.4f}/hr ({multiplier}x normal)",
                        "summary": (
                            f"Last hour gateway cost ${last:.4f} is {multiplier}x the "
                            f"6-hour rolling average of ${avg:.4f}/hr. "
                            f"Check if a new routing policy is directing traffic to a more expensive model, "
                            f"or if call volume has increased significantly."
                        ),
                        "default_severity": "warning",
                        "signal": {
                            "last_hour_cost":  last,
                            "avg_hourly_cost": avg,
                            "multiplier":      multiplier,
                        },
                    })
        except Exception as exc:
            log.warning("monitor_cost_spike_failed", error=str(exc))

        return anomalies

    # ── RCA generation ────────────────────────────────────────────────────────

    def _generate_rca(self, anomaly: dict) -> dict:
        signal_str = json.dumps({
            "anomaly_type":   anomaly["type"],
            "affected_agent": anomaly.get("agent"),
            "title":          anomaly["title"],
            "description":    anomaly.get("summary", ""),
            "signal_data":    anomaly.get("signal", {}),
        }, indent=2, default=str)

        try:
            from agent import generate_rca
            return generate_rca(signal_str)
        except Exception as exc:
            log.warning("rca_generation_failed", error=str(exc))
            sev = anomaly.get("default_severity", "medium")
            return {
                "severity":       sev,
                "summary":        anomaly.get("summary", ""),
                "rca":            "Automated RCA unavailable — check signal data.",
                "recommendation": "Review the affected agent's recent traces and governance logs.",
            }
