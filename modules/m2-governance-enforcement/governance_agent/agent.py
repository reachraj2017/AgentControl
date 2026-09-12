"""LiteLLM-backed agent with tool use loop for M2 Governance Agent."""

import json
import os
from typing import Any

import structlog

from tools import TOOL_SCHEMAS, execute_tool

log = structlog.get_logger()

SYSTEM_PROMPT = """You are the M2 Governance Agent — a specialized sub-agent for the AI Control Plane system.
You are an expert in AI governance: HITL approvals, circuit breakers, incidents, trust scores, rogue detection, policy decisions, and compliance.

Your responsibilities:
- HITL queue: list pending requests, approve, reject, or bulk-approve them
- Circuit breakers: check states (OPEN=blocked, CLOSED=healthy), reset, or quarantine agents
- Trust scores: review composite trust scores and history per agent
- Rogue assessments: detect anomalous agent behavior with quarantine recommendations
- Incidents: list open/resolved incidents, resolve individual or all open incidents
- Anomalies: surface statistical outliers in agent behavior
- Burn rates: monitor SLO error budget exhaustion
- Quality gates: review gate decisions (flag/hold/block) for output quality issues
- Gate audit log: inspect gate check statistics
- Policy violations: summarize policy engine blocks and flags
- Policy decisions: retrieve per-trace policy verdicts with metric/threshold context
- Findings: list, acknowledge, and resolve proactive monitor findings (single or bulk)
- Compliance report: generate governance compliance summary
- Reliability summary: surface uptime, error rate, and SLO compliance per agent

Always call get_hitl_queue, get_incidents, or get_findings before taking action on specific IDs — never ask the user for IDs.
For circuit breaker resets and quarantine actions, explain the reasoning and confirm with the user first.
For bulk operations (bulk_approve_hitl, bulk_resolve_incidents, bulk_resolve_findings), execute immediately when the user asks to clear/resolve all."""

_DEFAULT_AGENT_MODEL = "anthropic/claude-sonnet-4-6"


def _agent_model() -> str:
    return os.getenv("AGENT_MODEL", _DEFAULT_AGENT_MODEL)


def _litellm_kwargs(model: str) -> dict:
    """Return extra kwargs needed for the given model provider."""
    kwargs: dict = {}
    if "ollama" in model.lower():
        kwargs["api_base"] = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
        kwargs["extra_body"] = {"think": False}
    return kwargs


def _to_openai_tools(anthropic_tools: list[dict]) -> list[dict]:
    """Convert Anthropic tool schemas (input_schema) to OpenAI format (parameters)."""
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema", {"type": "object", "properties": {}}),
            },
        }
        for t in anthropic_tools
    ]


_OPENAI_TOOLS = _to_openai_tools(TOOL_SCHEMAS)


def chat(user_message: str, history: list[dict]) -> tuple[str, list[dict]]:
    """
    Run one chat turn with the governance agent.

    Args:
        user_message: the user's latest message
        history: prior conversation turns [{role, content}]

    Returns:
        (response_text, tool_calls_used)
        where tool_calls_used = [{name, inputs, result}]
    """
    import litellm

    messages: list[dict] = (
        [{"role": "system", "content": SYSTEM_PROMPT}]
        + list(history)
        + [{"role": "user", "content": user_message}]
    )
    tool_calls_used: list[dict] = []
    model = _agent_model()

    for _ in range(10):  # max 10 tool-call rounds
        response = litellm.completion(
            model=model,
            max_tokens=4096,
            tools=_OPENAI_TOOLS,
            messages=messages,
            **_litellm_kwargs(model),
        )

        choice = response.choices[0]
        tool_calls = choice.message.tool_calls or []

        if tool_calls or choice.finish_reason == "tool_calls":

            # Append assistant message with tool_calls for next round
            messages.append({
                "role": "assistant",
                "content": choice.message.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })

            # Execute each tool and append results
            for tc in tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}

                result = execute_tool(tc.function.name, args)
                tool_calls_used.append({
                    "name": tc.function.name,
                    "inputs": args,
                    "result": result[:500],
                })
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": result,
                })
                log.info("tool_called", tool=tc.function.name)

        else:
            return choice.message.content or "", tool_calls_used

    return "I reached the tool call limit. Please ask a more specific question.", tool_calls_used
