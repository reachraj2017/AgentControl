"""
acp_gateway.client
~~~~~~~~~~~~~~~~~~
Routes LLM API calls through the AI Control Plane Agent Gateway (M3).

The gateway is a drop-in proxy for OpenAI-compatible and Anthropic-compatible APIs.
It transparently adds caching, intelligent routing, shadow mode, A/B testing,
traffic management (endpoint pools), and governance enforcement.
"""

from __future__ import annotations

from typing import Any

import httpx


class GatewayClient:
    """
    Routes LLM calls through the ACP Agent Gateway.

    Args:
        gateway_url:     Base URL of the ACP Agent Gateway  (e.g. ``http://localhost:8080``).
        api_key:         Virtual gateway API key (``gw-sk-*``). Required when
                         ``GATEWAY_AUTH_ENABLED=true`` on the server.
        agent_role:      Role tag applied to every request — used for routing policies,
                         traffic management, governance checks, and observability.
        system_id:       Calling system identifier — scopes policies to a specific
                         deployment or product. Defaults to ``"*"`` (all systems).
        conversation_id: Stable ID for a multi-turn conversation. Required when the
                         traffic policy has ``sticky=true`` — ensures all turns in a
                         session hit the same pool endpoint.

    Example — OpenAI SDK drop-in::

        from acp_gateway import GatewayClient

        gw = GatewayClient(
            gateway_url="http://localhost:8080",
            api_key="gw-sk-...",
            agent_role="summarizer",
            system_id="my-product",
        )

        # Option A — use the GatewayClient directly
        resp = gw.chat_openai("gpt-4o-mini", [{"role": "user", "content": "Hello"}])
        print(resp["choices"][0]["message"]["content"])

        # Option B — point the official OpenAI SDK at the gateway (zero other changes)
        import openai
        client = gw.openai_client()
        resp = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": "Hello"}],
        )
    """

    def __init__(
        self,
        gateway_url: str = "http://localhost:8080",
        api_key: str = "",
        agent_role: str = "default",
        system_id: str = "*",
        conversation_id: str = "",
    ) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self.api_key = api_key
        self.agent_role = agent_role
        self.system_id = system_id
        self.conversation_id = conversation_id

    # ── LLM calls — OpenAI-compatible ──────────────────────────────────────────

    def chat_openai(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> dict:
        """
        POST /v1/chat/completions — OpenAI chat completion format.

        Returns the parsed JSON response or ``{"error": str}`` on failure.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            **kwargs,
        }
        return self._post("/v1/chat/completions", payload)

    def chat_anthropic(
        self,
        model: str,
        messages: list[dict],
        max_tokens: int = 2048,
        **kwargs: Any,
    ) -> dict:
        """
        POST /v1/messages — Anthropic message format.

        Returns the parsed JSON response or ``{"error": str}`` on failure.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            **kwargs,
        }
        return self._post("/v1/messages", payload)

    # ── SDK drop-in helpers ────────────────────────────────────────────────────

    def openai_client(self, **kwargs: Any) -> Any:
        """
        Return an ``openai.OpenAI`` client pre-configured to route through the gateway.

        All existing ``client.chat.completions.create(...)`` calls work unchanged.

        Raises:
            ImportError: if ``openai`` is not installed.
        """
        try:
            import openai  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "openai package is required: pip install openai"
            ) from exc
        return openai.OpenAI(
            api_key=self.api_key or "not-required",
            base_url=f"{self._gateway_url}/v1",
            default_headers=self._role_headers(),
            **kwargs,
        )

    def anthropic_client(self, **kwargs: Any) -> Any:
        """
        Return an ``anthropic.Anthropic`` client pre-configured to route through the gateway.

        All existing ``client.messages.create(...)`` calls work unchanged.

        Raises:
            ImportError: if ``anthropic`` is not installed.
        """
        try:
            import anthropic  # noqa: PLC0415
        except ImportError as exc:
            raise ImportError(
                "anthropic package is required: pip install anthropic"
            ) from exc
        return anthropic.Anthropic(
            api_key=self.api_key or "not-required",
            base_url=self._gateway_url,
            default_headers=self._role_headers(),
            **kwargs,
        )

    # ── Gateway status ─────────────────────────────────────────────────────────

    def health(self) -> bool:
        """Return ``True`` if the gateway is reachable."""
        try:
            with httpx.Client(timeout=5.0) as c:
                return c.get(f"{self._gateway_url}/health").status_code == 200
        except Exception:
            return False

    def get_call_stats(self, hours: int = 24) -> dict:
        """Fetch aggregate call stats for the last *hours* hours."""
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(
                    f"{self._gateway_url}/gateway/status",
                    params={"hours": hours},
                    headers=self._admin_headers(),
                )
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}

    def get_traffic_pools(self) -> dict:
        """List all endpoint pools and their members."""
        try:
            with httpx.Client(timeout=10.0) as c:
                r = c.get(
                    f"{self._gateway_url}/gateway/traffic/pools",
                    headers=self._admin_headers(),
                )
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            return {"error": str(exc)}

    # ── Internal ───────────────────────────────────────────────────────────────

    def _post(self, path: str, payload: dict) -> dict:
        try:
            with httpx.Client(timeout=120.0) as c:
                r = c.post(
                    f"{self._gateway_url}{path}",
                    json=payload,
                    headers=self._headers(),
                )
            r.raise_for_status()
            return r.json()
        except httpx.HTTPStatusError as exc:
            return {
                "error": f"HTTP {exc.response.status_code}: {exc.response.text}",
                "status_code": exc.response.status_code,
            }
        except Exception as exc:
            return {"error": str(exc)}

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {
            "Content-Type": "application/json",
            "X-Gateway-Agent-Role": self.agent_role,
            "X-Gateway-System-Id": self.system_id,
        }
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        if self.conversation_id:
            h["X-Gateway-Conversation-Id"] = self.conversation_id
        return h

    def _role_headers(self) -> dict[str, str]:
        """Headers injected into SDK clients (no Content-Type — the SDK sets it)."""
        h: dict[str, str] = {
            "X-Gateway-Agent-Role": self.agent_role,
            "X-Gateway-System-Id": self.system_id,
        }
        if self.conversation_id:
            h["X-Gateway-Conversation-Id"] = self.conversation_id
        return h

    def _admin_headers(self) -> dict[str, str]:
        h = self._role_headers()
        if self.api_key:
            h["X-Gateway-Admin-Key"] = self.api_key
        return h

    def __repr__(self) -> str:
        return (
            f"GatewayClient(gateway_url={self._gateway_url!r}, "
            f"agent_role={self.agent_role!r}, system_id={self.system_id!r})"
        )
