# Changelog

All notable changes to this project will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
This project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] — Initial release

### Added

**acp-gateway**
- `GatewayClient` — drop-in OpenAI and Anthropic SDK routing through the M3 Agent Gateway
- `openai_client()` / `anthropic_client()` — return properly configured SDK instances
- `chat_openai()` / `chat_anthropic()` — direct LLM calls via httpx
- `health()`, `get_call_stats()`, `get_traffic_pools()` — gateway status helpers
- Gateway headers: `X-Gateway-Agent-Role`, `X-Gateway-System-Id`, `X-Gateway-Conversation-Id`
- Sticky session support via `conversation_id`

**acp-tracing**
- `ACPTracer` — OTLP span export for M1 eval-runner
- Context manager `span()` and one-shot `record_call()` interfaces
- `trace()` decorator for function-level instrumentation
- Dual export strategy: opentelemetry-sdk (preferred) or direct httpx fallback
- Silent failure — tracing never crashes the host application

**acp-governance**
- `GovernanceClient` — M2 governance service client
- `check_policy()` — action-level policy gating
- `is_open()` / `get_circuit_breaker()` — circuit breaker state
- `get_trust_score()` — agent trust score lookup
- `request_approval()` / `get_approval_status()` / `wait_for_approval()` — HITL workflow
- `create_incident()` / `report_safety_event()` — incident and safety reporting

**acp-intelligence**
- `IntelligenceClient` — M4 EvalGov coordinator client
- `chat()` — natural language queries to the EvalGov coordinator
- Multi-turn conversation support via `history` parameter
- `get_findings()` — proactive monitor findings
- `trigger_monitor()` — run all 15 checks immediately
- `get_system_state()` — live control plane snapshot

**acp-sdk**
- `ACPClient` — unified client composing all four module clients
- Lazy initialization — module clients are only created when accessed
- `health()` — ping all reachable module endpoints
- Graceful degradation — modules whose packages aren't installed are silently skipped
- Re-exports all four client classes for direct import

**Examples**
- `basic_usage.py` — single traced gateway call
- `multi_agent_system.py` — orchestrator + specialist agents with governance gating
- `langgraph_integration.py` — LangGraph ReAct agent through the gateway
- `crewai_integration.py` — CrewAI crew with gateway-backed LLM

**CI/CD**
- GitHub Actions test matrix: all 5 packages × Python 3.9/3.11/3.12
- PyPI trusted publishing workflow on GitHub release
