"""ChangeStore — cached view of gateway policies from ClickHouse.

Refreshes every CHANGE_STORE_TTL_SECONDS (default 30).
All reads are non-blocking (return from in-memory cache).
The async refresh_loop() runs as a background task.
"""

import asyncio
import logging
import os
import random

from traffic import TrafficManager

log = logging.getLogger("gateway.change_store")

_TTL = float(os.getenv("CHANGE_STORE_TTL_SECONDS", "30"))


class ChangeStore:
    def __init__(self, db) -> None:
        self._db       = db
        self._routing:  list[dict] = []
        self._mods:     list[dict] = []
        self._shadow:   list[dict] = []
        self._ab_tests: list[dict] = []
        self._traffic_policies: list[dict] = []
        self._pools:            list[dict] = []
        self._pool_endpoints:   list[dict] = []
        self._traffic_mgr = TrafficManager()

    # ── Background refresh ────────────────────────────────────────────────────

    async def refresh_loop(self) -> None:
        while True:
            try:
                await self._refresh()
            except Exception as e:
                log.warning("change_store refresh error: %s", e)
            await asyncio.sleep(_TTL)

    async def _refresh(self) -> None:
        self._routing  = await asyncio.to_thread(self._db.get_routing_policies)
        self._mods     = await asyncio.to_thread(self._db.get_prompt_mods)
        self._shadow   = await asyncio.to_thread(self._db.get_shadow_rules)
        self._ab_tests = await asyncio.to_thread(self._db.get_ab_tests, "running")
        self._traffic_policies = await asyncio.to_thread(self._db.get_traffic_policies)
        self._pools            = await asyncio.to_thread(self._db.get_endpoint_pools)
        self._pool_endpoints   = await asyncio.to_thread(self._db.get_pool_endpoints)

    async def _refresh_traffic(self) -> None:
        """Refresh only the three traffic tables — fast targeted update for write endpoints."""
        self._traffic_policies = await asyncio.to_thread(self._db.get_traffic_policies)
        self._pools            = await asyncio.to_thread(self._db.get_endpoint_pools)
        self._pool_endpoints   = await asyncio.to_thread(self._db.get_pool_endpoints)
        log.debug(
            "change_store refreshed: %d routing, %d mods, %d shadow, %d ab_tests, "
            "%d traffic_policies, %d pools",
            len(self._routing), len(self._mods), len(self._shadow), len(self._ab_tests),
            len(self._traffic_policies), len(self._pools),
        )

    # ── Policy resolution (called per-request, must be fast) ─────────────────

    @staticmethod
    def _role_match(stored: str, incoming: str) -> bool:
        return stored == "*" or stored.lower() == incoming.lower()

    @staticmethod
    def _system_match(stored: str, incoming: str) -> bool:
        return stored == "*" or stored.lower() == incoming.lower()

    def resolve_routing(
        self,
        agent_role: str,
        system_id: str,
        requested_model: str,
    ) -> tuple[str, str, str, str, str]:
        """Return (target_model, target_backend, reason, fallback_model, fallback_backend).

        Policies are evaluated in order. First match wins.
        Wildcards ('*') match any value.
        """
        for p in self._routing:
            if not self._role_match(p["agent_role"], agent_role):
                continue
            if not self._system_match(p["system_id"], system_id):
                continue
            if p["model_match"] and p["model_match"] != requested_model:
                continue
            return (
                p["target_model"], p["target_backend"], p.get("reason", "policy"),
                p.get("fallback_model", ""), p.get("fallback_backend", "openai"),
            )
        return requested_model, "openai", "passthrough", "", "openai"

    def get_mods(self, agent_role: str, system_id: str) -> list[dict]:
        return [
            m for m in self._mods
            if self._role_match(m["agent_role"], agent_role)
            and self._system_match(m["system_id"], system_id)
        ]

    def get_shadow_rule(self, agent_role: str, system_id: str) -> dict | None:
        """Return a matching shadow rule sampled at its configured rate, or None."""
        for rule in self._shadow:
            if not self._role_match(rule["agent_role"], agent_role):
                continue
            if not self._system_match(rule["system_id"], system_id):
                continue
            if random.random() < float(rule.get("sample_rate", 0.1)):
                return rule
        return None

    def get_active_ab_test(self, agent_role: str, system_id: str) -> dict | None:
        """Return the first running A/B test matching this agent_role/system_id, or None."""
        for test in self._ab_tests:
            if not self._role_match(test["agent_role"], agent_role):
                continue
            if not self._system_match(test["system_id"], system_id):
                continue
            return test
        return None

    # ── Traffic policy resolution ─────────────────────────────────────────────

    def get_traffic_policy(self, agent_role: str, system_id: str) -> dict | None:
        """Return the first enabled traffic policy matching this agent_role/system_id."""
        for p in self._traffic_policies:
            if not self._role_match(p["agent_role"], agent_role):
                continue
            if not self._system_match(p["system_id"], system_id):
                continue
            return p
        return None

    def select_pool_endpoint(
        self,
        policy:          dict,
        agent_role:      str,
        conversation_id: str = "",
    ) -> dict | None:
        """Use TrafficManager to select an endpoint from the policy's pool."""
        pool_id  = policy.get("pool_id", "")
        pool     = next(
            (p for p in self._pools if p["pool_id"] == pool_id and p.get("enabled")),
            None,
        )
        if not pool:
            return None
        endpoints = [e for e in self._pool_endpoints if e["pool_id"] == pool_id]
        return self._traffic_mgr.select(
            pool=pool,
            endpoints=endpoints,
            db=self._db,
            agent_role=agent_role,
            conversation_id=conversation_id,
            sticky=bool(policy.get("sticky", 0)),
        )

    # ── Snapshot (for ops surface) ────────────────────────────────────────────

    @property
    def routing(self) -> list[dict]:
        return list(self._routing)

    @property
    def mods(self) -> list[dict]:
        return list(self._mods)

    @property
    def shadow(self) -> list[dict]:
        return list(self._shadow)

    @property
    def ab_tests(self) -> list[dict]:
        return list(self._ab_tests)

    @property
    def traffic_policies(self) -> list[dict]:
        return list(self._traffic_policies)

    @property
    def pools(self) -> list[dict]:
        return list(self._pools)

    @property
    def pool_endpoints(self) -> list[dict]:
        return list(self._pool_endpoints)
