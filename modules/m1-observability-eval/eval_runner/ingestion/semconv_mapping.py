"""
Semantic convention normalization layer — the "Rosetta stone" between M1's
internal span shape and the industry-standard dialects an external, unmodified
agent framework might already emit:

  1. OTel GenAI Semantic Conventions — span names like "chat <model>",
     "generate_content <model>"; gen_ai.system / gen_ai.request.model /
     gen_ai.usage.input_tokens / gen_ai.usage.output_tokens /
     gen_ai.prompt / gen_ai.completion; operation names invoke_agent /
     execute_tool.
  2. OpenInference (Arize) — openinference.span.kind in
     {LLM, CHAIN, AGENT, TOOL, RETRIEVER}; input.value / output.value;
     llm.token_count.prompt / llm.token_count.completion; llm.model_name.
  3. OpenLLMetry (Traceloop) — largely OTel-GenAI-aligned attributes plus
     some traceloop.* attributes.
  4. ACP native — this system's own span shape (agent.task / agent.tool_call /
     agent.handoff / llm_call / agent.llm_call / generate_content / openai.chat),
     kept as the always-recognized baseline and fallback.

Any of these four dialects triggers evaluation and extracts
correctly, not just ACP's own convention. Because these three external
dialects are actively converging toward one OTel standard, this mapping table
is expected to shrink over time, not grow the way a bespoke per-framework
adapter list would.

Nothing here talks to the network or ClickHouse — it's a pure function over
a single span dict, used by both trigger.py (does this span start an eval?)
and trace_assembler.py (how do I read tokens/prompt/completion off it?).
"""

from __future__ import annotations

from typing import Optional

# ----------------------------------------------------------------------
# Recognized "kinds" — the normalized vocabulary the rest of M1 speaks.
# ----------------------------------------------------------------------
KIND_AGENT_TASK = "agent_task"
KIND_LLM_CALL = "llm_call"
KIND_TOOL_CALL = "tool_call"
KIND_HANDOFF = "handoff"

_ACP_TASK_NAMES = {"agent.task", "invoke_agent", "agent.run"}
_ACP_TOOL_NAMES = {"agent.tool_call", "execute_tool"}
_ACP_HANDOFF_NAMES = {"agent.handoff"}
_ACP_LLM_NAME_SUBSTRINGS = ("llm", "generate_content", "chat")
_ACP_LLM_NAME_EXCLUDE = {"call_llm", "agent.llm_call"}  # ADK wrapper / manual span, see trace_assembler


def _first(attrs: dict, *keys: str):
    for k in keys:
        if k in attrs and attrs[k] not in (None, ""):
            return attrs[k]
    return None


def _as_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _empty_result(raw_dialect: str, kind: str) -> dict:
    return {
        "kind": kind,
        "model": None,
        "tokens_in": None,
        "tokens_out": None,
        "prompt": None,
        "completion": None,
        "tool_name": None,
        "raw_dialect": raw_dialect,
    }


# ----------------------------------------------------------------------
# Dialect 1 — OTel GenAI Semantic Conventions
# ----------------------------------------------------------------------

def _try_otel_genai(name: str, attrs: dict) -> Optional[dict]:
    operation = attrs.get("gen_ai.operation.name", "")

    if operation == "invoke_agent":
        return _empty_result("otel_genai", KIND_AGENT_TASK)

    if operation == "execute_tool" or name.startswith("execute_tool"):
        r = _empty_result("otel_genai", KIND_TOOL_CALL)
        r["tool_name"] = _first(attrs, "gen_ai.tool.name")
        return r

    has_genai_attrs = any(k.startswith("gen_ai.") for k in attrs)
    is_genai_span_name = (
        name.startswith("chat ")
        or name.startswith("generate_content")
        or operation in ("chat", "generate_content", "text_completion")
    )
    if has_genai_attrs or is_genai_span_name:
        r = _empty_result("otel_genai", KIND_LLM_CALL)
        r["model"] = _first(attrs, "gen_ai.request.model", "gen_ai.response.model")
        r["tokens_in"] = _as_int(_first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"))
        r["tokens_out"] = _as_int(_first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"))
        r["prompt"] = _first(attrs, "gen_ai.prompt")
        r["completion"] = _first(attrs, "gen_ai.completion")
        return r

    return None


# ----------------------------------------------------------------------
# Dialect 2 — OpenInference (Arize)
# ----------------------------------------------------------------------

def _try_openinference(name: str, attrs: dict) -> Optional[dict]:
    span_kind = attrs.get("openinference.span.kind", "")
    if not span_kind:
        return None

    span_kind = span_kind.upper()
    if span_kind in ("AGENT", "CHAIN"):
        return _empty_result("openinference", KIND_AGENT_TASK)

    if span_kind == "TOOL":
        r = _empty_result("openinference", KIND_TOOL_CALL)
        r["tool_name"] = _first(attrs, "tool.name")
        return r

    if span_kind == "LLM":
        r = _empty_result("openinference", KIND_LLM_CALL)
        r["model"] = _first(attrs, "llm.model_name")
        r["tokens_in"] = _as_int(_first(attrs, "llm.token_count.prompt"))
        r["tokens_out"] = _as_int(_first(attrs, "llm.token_count.completion"))
        r["prompt"] = _first(attrs, "input.value")
        r["completion"] = _first(attrs, "output.value")
        return r

    # RETRIEVER and other kinds aren't eval-target-relevant on their own.
    return None


# ----------------------------------------------------------------------
# Dialect 3 — OpenLLMetry (Traceloop)
# ----------------------------------------------------------------------

def _try_openllmetry(name: str, attrs: dict) -> Optional[dict]:
    has_traceloop_attrs = any(k.startswith("traceloop.") for k in attrs)
    workflow_or_task = attrs.get("traceloop.span.kind", "")

    if workflow_or_task in ("workflow", "task", "agent"):
        return _empty_result("openllmetry", KIND_AGENT_TASK)

    if workflow_or_task == "tool":
        r = _empty_result("openllmetry", KIND_TOOL_CALL)
        r["tool_name"] = _first(attrs, "traceloop.entity.name")
        return r

    # OpenLLMetry's LLM spans are OTel-GenAI-attribute-aligned; only treat as
    # a distinct dialect hit when a traceloop.* attribute is present alongside
    # gen_ai.* ones (otherwise dialect 1 already matched it).
    if has_traceloop_attrs and any(k.startswith("gen_ai.") for k in attrs):
        r = _empty_result("openllmetry", KIND_LLM_CALL)
        r["model"] = _first(attrs, "gen_ai.request.model", "gen_ai.response.model")
        r["tokens_in"] = _as_int(_first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"))
        r["tokens_out"] = _as_int(_first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"))
        r["prompt"] = _first(attrs, "gen_ai.prompt")
        r["completion"] = _first(attrs, "gen_ai.completion")
        return r

    return None


# ----------------------------------------------------------------------
# Dialect 4 — ACP native (this system's own span shape)
# ----------------------------------------------------------------------

def _try_acp_native(name: str, attrs: dict) -> Optional[dict]:
    if name in _ACP_TASK_NAMES:
        return _empty_result("acp_native", KIND_AGENT_TASK)

    if name in _ACP_HANDOFF_NAMES:
        return _empty_result("acp_native", KIND_HANDOFF)

    if name in _ACP_TOOL_NAMES:
        r = _empty_result("acp_native", KIND_TOOL_CALL)
        r["tool_name"] = _first(attrs, "tool.name")
        return r

    name_lower = name.lower()
    if name not in _ACP_LLM_NAME_EXCLUDE and any(s in name_lower for s in _ACP_LLM_NAME_SUBSTRINGS):
        r = _empty_result("acp_native", KIND_LLM_CALL)
        r["model"] = _first(attrs, "gen_ai.request.model", "llm.model")
        r["tokens_in"] = _as_int(_first(attrs, "gen_ai.usage.input_tokens", "gen_ai.usage.prompt_tokens"))
        r["tokens_out"] = _as_int(_first(attrs, "gen_ai.usage.output_tokens", "gen_ai.usage.completion_tokens"))
        r["prompt"] = _first(attrs, "gen_ai.prompt")
        r["completion"] = _first(attrs, "gen_ai.completion")
        return r

    return None


# ----------------------------------------------------------------------
# Public API
# ----------------------------------------------------------------------

_DIALECT_PROBES = (
    _try_openinference,   # most specific marker attribute (openinference.span.kind) first
    _try_openllmetry,     # traceloop.* marker second
    _try_otel_genai,      # gen_ai.* / operation.name — broad standard, checked before native fallback
    _try_acp_native,      # ACP's own literal span-name conventions — last, catch-all
)


def normalize_span(span: dict) -> Optional[dict]:
    """
    Try each supported dialect against a raw span and return a normalized
    result, or None if no dialect recognizes it.

    `span` is expected in this system's flat span-dict shape:
        {"span_name": str, "attributes": dict, ...}
    (i.e. the shape returned by Repository.get_trace_spans / hint_spans from
    the OTLP receiver — NOT a raw OTel protobuf/JSON span.)
    """
    name = span.get("span_name", "") or ""
    attrs = span.get("attributes", {}) or {}

    for probe in _DIALECT_PROBES:
        result = probe(name, attrs)
        if result is not None:
            return result
    return None


def is_agent_task_span(span: dict) -> bool:
    """True if this span (any recognized dialect) represents an agent task boundary."""
    result = normalize_span(span)
    return result is not None and result["kind"] == KIND_AGENT_TASK


def is_llm_span(span: dict) -> bool:
    result = normalize_span(span)
    return result is not None and result["kind"] == KIND_LLM_CALL


def is_tool_span(span: dict) -> bool:
    result = normalize_span(span)
    return result is not None and result["kind"] == KIND_TOOL_CALL


def is_handoff_span(span: dict) -> bool:
    result = normalize_span(span)
    return result is not None and result["kind"] == KIND_HANDOFF


def register_task_span_names(names) -> None:
    """
    Extend the set of literal span names recognized as an agent-task boundary
    under the acp_native dialect, beyond the built-in agent.task/invoke_agent/
    agent.run. Called at startup from config/evaluator_config.yaml's
    ingestion.task_span_names, so an operator can add a proprietary or
    framework-specific span name without a code change.
    """
    _ACP_TASK_NAMES.update(n for n in names if n)


# ----------------------------------------------------------------------
# Conformance self-test — one fixture per dialect.
# Run directly:  python3 ingestion/semconv_mapping.py
# ----------------------------------------------------------------------

_FIXTURES = [
    (
        "acp_native agent.task",
        {"span_name": "agent.task", "attributes": {"agent.role": "orchestrator"}},
        {"kind": KIND_AGENT_TASK, "raw_dialect": "acp_native"},
    ),
    (
        "generate_content w/ gen_ai attrs (ADK — legitimately otel_genai-conventional)",
        {
            "span_name": "generate_content openai/gpt-4o-mini",
            "attributes": {
                "gen_ai.request.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": "120",
                "gen_ai.usage.output_tokens": "42",
            },
        },
        {"kind": KIND_LLM_CALL, "raw_dialect": "otel_genai", "model": "gpt-4o-mini",
         "tokens_in": 120, "tokens_out": 42},
    ),
    (
        "acp_native openai.chat (manual instrumentation, no gen_ai.* attrs)",
        {"span_name": "openai.chat", "attributes": {"llm.model": "gpt-4o-mini"}},
        {"kind": KIND_LLM_CALL, "raw_dialect": "acp_native", "model": "gpt-4o-mini"},
    ),
    (
        "otel_genai invoke_agent",
        {
            "span_name": "invoke_agent orchestrator",
            "attributes": {"gen_ai.operation.name": "invoke_agent", "gen_ai.system": "openai"},
        },
        {"kind": KIND_AGENT_TASK, "raw_dialect": "otel_genai"},
    ),
    (
        "otel_genai chat span",
        {
            "span_name": "chat gpt-4o-mini",
            "attributes": {
                "gen_ai.system": "openai",
                "gen_ai.request.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 200,
                "gen_ai.usage.output_tokens": 55,
                "gen_ai.prompt": "hello",
                "gen_ai.completion": "hi there",
            },
        },
        {"kind": KIND_LLM_CALL, "raw_dialect": "otel_genai", "model": "gpt-4o-mini",
         "tokens_in": 200, "tokens_out": 55, "prompt": "hello", "completion": "hi there"},
    ),
    (
        "openinference AGENT span",
        {"span_name": "LangChain AgentExecutor", "attributes": {"openinference.span.kind": "AGENT"}},
        {"kind": KIND_AGENT_TASK, "raw_dialect": "openinference"},
    ),
    (
        "openinference LLM span",
        {
            "span_name": "ChatOpenAI",
            "attributes": {
                "openinference.span.kind": "LLM",
                "llm.model_name": "gpt-4o",
                "llm.token_count.prompt": 300,
                "llm.token_count.completion": 80,
                "input.value": "what's the weather",
                "output.value": "sunny",
            },
        },
        {"kind": KIND_LLM_CALL, "raw_dialect": "openinference", "model": "gpt-4o",
         "tokens_in": 300, "tokens_out": 80, "prompt": "what's the weather", "completion": "sunny"},
    ),
    (
        "openinference TOOL span",
        {
            "span_name": "web_search",
            "attributes": {"openinference.span.kind": "TOOL", "tool.name": "web_search"},
        },
        {"kind": KIND_TOOL_CALL, "raw_dialect": "openinference", "tool_name": "web_search"},
    ),
    (
        "openllmetry workflow span",
        {
            "span_name": "customer_service.workflow",
            "attributes": {"traceloop.span.kind": "workflow", "traceloop.entity.name": "customer_service"},
        },
        {"kind": KIND_AGENT_TASK, "raw_dialect": "openllmetry"},
    ),
    (
        "openllmetry LLM span",
        {
            "span_name": "openai.chat",
            "attributes": {
                "traceloop.entity.name": "openai",
                "gen_ai.request.model": "gpt-4o-mini",
                "gen_ai.usage.input_tokens": 90,
                "gen_ai.usage.output_tokens": 30,
            },
        },
        {"kind": KIND_LLM_CALL, "raw_dialect": "openllmetry", "model": "gpt-4o-mini",
         "tokens_in": 90, "tokens_out": 30},
    ),
    (
        "unrecognized span",
        {"span_name": "some.random.internal.span", "attributes": {"foo": "bar"}},
        None,
    ),
]


def _run_self_test() -> None:
    failures = 0
    for label, span, expected in _FIXTURES:
        result = normalize_span(span)
        if expected is None:
            ok = result is None
        else:
            ok = result is not None and all(result.get(k) == v for k, v in expected.items())
        status = "PASS" if ok else "FAIL"
        if not ok:
            failures += 1
        print(f"[{status}] {label}: got={result}")
    print(f"\n{len(_FIXTURES) - failures}/{len(_FIXTURES)} fixtures passed")
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    _run_self_test()
