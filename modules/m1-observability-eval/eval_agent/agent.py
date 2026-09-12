"""LiteLLM-backed agent with tool use loop for M1 Eval Agent."""

import json
import os
from typing import Any

import structlog

from tools import TOOL_SCHEMAS, execute_tool

log = structlog.get_logger()

SYSTEM_PROMPT = """You are the M1 Eval Agent — a specialized sub-agent for the AI Control Plane system.
You are an expert in eval testing, benchmark runs, prompt analysis, agent performance metrics, costs, OTel traces, and compliance data.

Your responsibilities:
- Eval runs: list, create, execute, and re-evaluate benchmark runs
- Benchmarks: list, create, and manage test cases
- Eval scores: retrieve detailed scores with reasoning, compare metrics across agents
- Traces: query recent OTel agent.task spans, diagnose performance issues
- Cost breakdown: analyze token usage and USD cost by agent and source
- Error rates: surface error patterns per agent role
- Safety events: review safety rule violations and matched content
- Thresholds and budgets: inspect governance thresholds, agent token/cost budgets, version pins
- Lifecycle changes: audit configuration change log
- Model registry: check approved models
- Compliance scorecard: report on SOC2/GDPR/HIPAA control status
- Risk register: surface open risk items
- Prompts: search prompt/response pairs, retrieve full prompt detail for a trace

Always call tools before answering — never guess numbers.
Use wide time windows (hours=720 for 30 days) if 24h returns empty — agent traces may be days or weeks old.
Always state the actual time window used in your answer.
If get_recent_traces, get_cost_breakdown, get_agent_performance, or get_error_rates return empty,
ALWAYS retry with hours=720 before concluding there is no data."""

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
    Run one chat turn with the eval agent.

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
