"""TrafficManager — endpoint pool selection for the agent gateway.

Strategies:
  round_robin      — rotate through enabled endpoints evenly
  weighted         — probabilistic selection by endpoint.weight
  least_latency    — pick lowest avg_latency_ms (gateway_call_log last 5 min)
  performance      — pick highest avg_faithfulness (gateway_shadow_evals last 1h)
  cost_optimized   — pick cheapest model (estimated by token price)
  fallback_chain   — try endpoints in ascending priority order
"""

import logging
import random
import threading

log = logging.getLogger("gateway.traffic")

# Rough relative cost rank per 1M tokens (blended in+out). Lower = cheaper.
_COST_RANK: dict[str, float] = {
    "ollama":    0.0,
    "haiku":     0.3,
    "gpt-4o-mini": 0.5,
    "sonnet":    3.0,
    "gpt-4o":    5.0,
    "opus":     15.0,
    "gpt-4":    10.0,
}


def _model_cost_rank(model: str) -> float:
    ml = model.lower()
    for key, cost in sorted(_COST_RANK.items(), key=lambda x: x[1]):
        if key in ml:
            return cost
    return 5.0


class TrafficManager:
    """Selects an endpoint from a pool using the pool's configured strategy.

    Instance is shared across requests — all state is protected by a lock.
    """

    def __init__(self) -> None:
        self._lock         = threading.Lock()
        self._rr_counters: dict[str, int]  = {}
        # sticky sessions: conversation_id → endpoint_id (in-memory, no TTL)
        self._sticky_map:  dict[str, str]  = {}

    def select(
        self,
        pool:            dict,
        endpoints:       list[dict],
        db,
        agent_role:      str,
        conversation_id: str  = "",
        sticky:          bool = False,
    ) -> dict | None:
        enabled = [e for e in endpoints if e.get("enabled", 1)]
        if not enabled:
            return None

        pool_id  = pool.get("pool_id", "")
        strategy = pool.get("strategy", "round_robin")

        if sticky and conversation_id:
            pinned = self._sticky_map.get(conversation_id)
            if pinned:
                ep = next((e for e in enabled if e["endpoint_id"] == pinned), None)
                if ep:
                    return ep

        if strategy == "round_robin":
            ep = self._round_robin(pool_id, enabled)
        elif strategy == "weighted":
            ep = self._weighted(enabled)
        elif strategy == "least_latency":
            ep = self._least_latency(enabled, db, agent_role)
        elif strategy == "performance":
            ep = self._performance(enabled, db, agent_role)
        elif strategy == "cost_optimized":
            ep = self._cost_optimized(enabled)
        elif strategy == "fallback_chain":
            ep = self._fallback_chain(enabled)
        else:
            ep = enabled[0]

        if sticky and conversation_id and ep:
            with self._lock:
                self._sticky_map[conversation_id] = ep["endpoint_id"]

        return ep

    # ── Strategies ────────────────────────────────────────────────────────────

    def _round_robin(self, pool_id: str, endpoints: list[dict]) -> dict:
        with self._lock:
            idx = self._rr_counters.get(pool_id, 0)
            self._rr_counters[pool_id] = idx + 1
        return endpoints[idx % len(endpoints)]

    def _weighted(self, endpoints: list[dict]) -> dict:
        weights = [max(float(e.get("weight", 1.0)), 0.0) for e in endpoints]
        total   = sum(weights) or 1.0
        r       = random.random() * total
        cumsum  = 0.0
        for ep, w in zip(endpoints, weights):
            cumsum += w
            if r <= cumsum:
                return ep
        return endpoints[-1]

    def _fallback_chain(self, endpoints: list[dict]) -> dict:
        return sorted(endpoints, key=lambda e: int(e.get("priority", 1)))[0]

    def _least_latency(self, endpoints: list[dict], db, agent_role: str) -> dict:
        stats = self._fetch_stats(endpoints, db, agent_role)
        best, best_lat = None, float("inf")
        for ep in endpoints:
            lat = stats.get(ep["model"], {}).get("avg_latency_ms", 9999.0)
            if lat < best_lat:
                best_lat, best = lat, ep
        return best or endpoints[0]

    def _performance(self, endpoints: list[dict], db, agent_role: str) -> dict:
        stats = self._fetch_stats(endpoints, db, agent_role)
        best, best_score = None, -1.0
        for ep in endpoints:
            score = stats.get(ep["model"], {}).get("avg_faithfulness", 0.0)
            if score > best_score:
                best_score, best = score, ep
        return best or endpoints[0]

    def _cost_optimized(self, endpoints: list[dict]) -> dict:
        return sorted(endpoints, key=lambda e: _model_cost_rank(e.get("model", "")))[0]

    def _fetch_stats(
        self, endpoints: list[dict], db, agent_role: str
    ) -> dict[str, dict]:
        try:
            return db.get_pool_endpoint_stats(agent_role, [e["model"] for e in endpoints])
        except Exception:
            return {}
