"""
acp_sdk.client
~~~~~~~~~~~~~~
ACPClient — single-object access to the entire AI Control Plane.

Composes all four module clients (M1–M4) with a shared base configuration.
Any module whose package is not installed is silently skipped — you can use
ACPClient with only the modules you have deployed.
"""

from __future__ import annotations

from typing import Any


class ACPClient:
    """
    Unified client for all four AI Control Plane modules.

    Args:
        gateway_url:     M3 Agent Gateway base URL.
        eval_runner_url: M1 Eval Runner OTLP endpoint.
        governance_url:  M2 Governance Service base URL.
        evalgov_url:     M4 EvalGov Coordinator base URL.
        api_key:         Virtual gateway API key (``gw-sk-*``).
        agent_name:      Agent name — used for tracing spans and governance lookups.
        agent_role:      Role tag — used for gateway routing and governance policies.
        system_id:       System/deployment identifier — scopes all policies.
        conversation_id: Stable session ID for sticky gateway traffic policies.

    Example::

        from acp_sdk import ACPClient

        acp = ACPClient(
            gateway_url="http://localhost:8080",
            eval_runner_url="http://localhost:8000",
            governance_url="http://localhost:8002",
            evalgov_url="http://localhost:8003",
            api_key="gw-sk-...",
            agent_name="summarizer",
            agent_role="summarizer",
            system_id="my-product",
        )

        # Route calls through the gateway (M3)
        client = acp.gateway.openai_client()

        # Instrument with OTLP tracing (M1)
        with acp.tracer.span(model="gpt-4o-mini") as ctx:
            ctx.prompt = prompt
            resp = client.chat.completions.create(model="gpt-4o-mini", messages=[...])
            ctx.completion = resp.choices[0].message.content
            ctx.tokens_in = resp.usage.prompt_tokens
            ctx.tokens_out = resp.usage.completion_tokens

        # Gate with governance policy (M2)
        decision = acp.governance.check_policy("send_email", {"recipient": "x@y.com"})

        # Query the control plane via EvalGov (M4)
        answer = acp.intelligence.chat("What is the current system health?")
    """

    def __init__(
        self,
        gateway_url: str = "http://localhost:8080",
        eval_runner_url: str = "http://localhost:8000",
        governance_url: str = "http://localhost:8002",
        evalgov_url: str = "http://localhost:8003",
        api_key: str = "",
        agent_name: str = "agent",
        agent_role: str = "default",
        system_id: str = "*",
        conversation_id: str = "",
    ) -> None:
        self._cfg = {
            "gateway_url": gateway_url,
            "eval_runner_url": eval_runner_url,
            "governance_url": governance_url,
            "evalgov_url": evalgov_url,
            "api_key": api_key,
            "agent_name": agent_name,
            "agent_role": agent_role,
            "system_id": system_id,
            "conversation_id": conversation_id,
        }
        self._gateway: Any = None
        self._tracer: Any = None
        self._governance: Any = None
        self._intelligence: Any = None
        self._signals: Any = None

    # ── Lazy module accessors ─────────────────────────────────────────────────

    @property
    def gateway(self) -> Any:
        """
        :class:`acp_gateway.GatewayClient` instance.

        Raises:
            ImportError: if ``acp-gateway`` is not installed.
        """
        if self._gateway is None:
            try:
                from acp_gateway import GatewayClient  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError("pip install acp-gateway") from exc
            self._gateway = GatewayClient(
                gateway_url=self._cfg["gateway_url"],
                api_key=self._cfg["api_key"],
                agent_role=self._cfg["agent_role"],
                system_id=self._cfg["system_id"],
                conversation_id=self._cfg["conversation_id"],
            )
        return self._gateway

    @property
    def tracer(self) -> Any:
        """
        :class:`acp_tracing.ACPTracer` instance.

        Raises:
            ImportError: if ``acp-tracing`` is not installed.
        """
        if self._tracer is None:
            try:
                from acp_tracing import ACPTracer  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError("pip install acp-tracing") from exc
            self._tracer = ACPTracer(
                otlp_endpoint=self._cfg["eval_runner_url"],
                agent_name=self._cfg["agent_name"],
                agent_role=self._cfg["agent_role"],
                system_id=self._cfg["system_id"],
            )
        return self._tracer

    @property
    def governance(self) -> Any:
        """
        :class:`acp_governance.GovernanceClient` instance.

        Raises:
            ImportError: if ``acp-governance`` is not installed.
        """
        if self._governance is None:
            try:
                from acp_governance import GovernanceClient  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError("pip install acp-governance") from exc
            self._governance = GovernanceClient(
                governance_url=self._cfg["governance_url"],
                agent_name=self._cfg["agent_name"],
                system_id=self._cfg["system_id"],
            )
        return self._governance

    @property
    def intelligence(self) -> Any:
        """
        :class:`acp_intelligence.IntelligenceClient` instance.

        Raises:
            ImportError: if ``acp-intelligence`` is not installed.
        """
        if self._intelligence is None:
            try:
                from acp_intelligence import IntelligenceClient  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError("pip install acp-intelligence") from exc
            self._intelligence = IntelligenceClient(
                evalgov_url=self._cfg["evalgov_url"],
            )
        return self._intelligence

    @property
    def signals(self) -> Any:
        """
        :class:`acp_signals.SignalsClient` instance — explicit checkpoint/handoff/tool-span
        calls for the signals that never cross the LLM wire (see ``acp-signals`` package).

        Raises:
            ImportError: if ``acp-signals`` is not installed.
        """
        if self._signals is None:
            try:
                from acp_signals import SignalsClient  # noqa: PLC0415
            except ImportError as exc:
                raise ImportError("pip install acp-signals") from exc
            self._signals = SignalsClient(
                gateway_url=self._cfg["gateway_url"],
                api_key=self._cfg["api_key"],
            )
        return self._signals

    # ── Convenience ───────────────────────────────────────────────────────────

    def health(self) -> dict[str, bool]:
        """
        Ping all reachable module endpoints.

        Returns a dict like ``{"gateway": True, "governance": True, "evalgov": True}``.
        Only checks modules whose packages are installed.
        """
        result: dict[str, bool] = {}
        try:
            result["gateway"] = self.gateway.health()
        except ImportError:
            pass
        try:
            result["governance"] = self.governance.health()
        except ImportError:
            pass
        try:
            result["evalgov"] = self.intelligence.health()
        except ImportError:
            pass
        return result

    def __repr__(self) -> str:
        return (
            f"ACPClient(agent_name={self._cfg['agent_name']!r}, "
            f"agent_role={self._cfg['agent_role']!r}, "
            f"system_id={self._cfg['system_id']!r})"
        )
