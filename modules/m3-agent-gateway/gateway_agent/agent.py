"""LiteLLM-backed agent with tool use loop for M3 Gateway Agent."""

import json
import os
from typing import Any

import structlog

from tools import TOOL_SCHEMAS, execute_tool

log = structlog.get_logger()

SYSTEM_PROMPT = """You are the M3 Gateway Agent — a specialized sub-agent for the AI Control Plane system.
You are an expert in gateway configuration: routing policies, A/B tests, shadow rules, prompt modifications, API keys, change proposals, and traffic management (endpoint pools).

Your responsibilities:
- Gateway call stats: analyze per-(agent_role, model) call volume, latency, tokens, and eval scores
- Routing decisions: inspect model routing choices and cost savings
- Gateway proposals: list pending/approved/rejected change proposals, submit new proposals
- A/B tests: list running/completed tests, retrieve per-variant results, decide winners
- Shadow rules: list, create, and delete shadow traffic rules
- Prompt mods: list, create, and delete prompt modification rules (prefix/suffix/few-shot)
- API keys: list active keys, check rejection events, update key config, create new keys, revoke keys
- Routing policies: list, create, and delete routing policies directly
- Traffic management: create/delete endpoint pools, add/remove pool endpoints, bind pools to agent roles via policies, and query live pool stats

Always call list tools (list_routing_policies, list_ab_tests, get_gateway_keys, list_traffic_pools, list_traffic_policies, etc.) before delete/update operations — never ask the user for IDs.

For routing changes and model promotions:
1. Call get_gateway_call_stats() to gather quality/cost evidence
2. Check compare_shadow_vs_primary() if shadow rules exist
3. Call get_gateway_proposals(status='pending') to avoid duplicates before proposing
4. Use propose_gateway_change() for changes requiring operator review
5. Use create_routing_policy() for direct immediate enforcement

Traffic management workflow (pools take priority over routing policies and A/B tests):
1. call list_traffic_pools() to inspect existing pools and endpoints
2. call create_traffic_pool(name, strategy) to create a new pool
3. call add_pool_endpoint(pool_id, model, backend, weight, priority) once per model
4. call create_traffic_policy(pool_id, agent_role) to activate routing
5. call get_traffic_stats(hours) to verify traffic is flowing and check per-endpoint error rates

Strategies to recommend:
- round_robin: equal traffic across all endpoints (default, good for identical models)
- weighted: proportional traffic by weight (use when models differ in capacity/cost)
- least_latency: always route to the fastest endpoint (last 5 min avg)
- performance: route to the endpoint with the best eval scores
- cost_optimized: prefer cheaper models, use expensive ones as overflow
- fallback_chain: try endpoints in priority order, move to next on failure

To remove traffic management: delete the traffic policy first (unbinds routing), then delete the pool if no longer needed.

Winner criterion for A/B tests: one variant scores >0.05 higher on 2+ eval metrics AND both variants have ≥30 calls.
Always include quantitative evidence (call counts, score deltas, latency) in proposal evidence fields.

For shadow mode: recommend shadow rules when you want to evaluate a model without committing to production traffic."""

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
    Run one chat turn with the gateway agent.

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
