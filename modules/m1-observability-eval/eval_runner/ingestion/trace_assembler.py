"""
Assembles a full trace tree from individual spans stored in ClickHouse.
Reconstructs parent-child relationships.
"""

from typing import Optional

import structlog

from db.repository import Repository
from ingestion.semconv_mapping import (
    KIND_AGENT_TASK,
    KIND_HANDOFF,
    KIND_LLM_CALL,
    KIND_TOOL_CALL,
    normalize_span,
)

log = structlog.get_logger(__name__)


def _with_canonical_llm_attrs(span: dict, norm: dict) -> dict:
    """
    Return a shallow copy of an LLM-call span with canonical gen_ai.* keys
    filled in from the normalized (dialect-agnostic) result, without
    overwriting any gen_ai.* key already present.

    Downstream code (EvalPipeline._save_otel_computed_metrics,
    _save_prompt_eval) reads model/token attributes via gen_ai.request.model /
    gen_ai.usage.input_tokens / gen_ai.usage.output_tokens / llm.model — this
    is what lets an OpenInference (llm.model_name, llm.token_count.*) or
    OpenLLMetry-only span feed those same code paths unchanged.
    """
    attrs = dict(span.get("attributes", {}) or {})
    if norm.get("model") is not None:
        attrs.setdefault("gen_ai.request.model", norm["model"])
    if norm.get("tokens_in") is not None:
        attrs.setdefault("gen_ai.usage.input_tokens", norm["tokens_in"])
    if norm.get("tokens_out") is not None:
        attrs.setdefault("gen_ai.usage.output_tokens", norm["tokens_out"])
    if norm.get("prompt") is not None:
        attrs.setdefault("gen_ai.prompt", norm["prompt"])
    if norm.get("completion") is not None:
        attrs.setdefault("gen_ai.completion", norm["completion"])
    new_span = dict(span)
    new_span["attributes"] = attrs
    return new_span


class TraceAssembler:
    """Reconstructs a trace tree from flat span records."""

    def __init__(self, repository: Optional[Repository] = None) -> None:
        self._repo = repository or Repository()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def assemble(self, trace_id: str) -> dict:
        """
        Build a tree structure for a trace.

        Returns:
            {"span": span_dict, "children": [tree_node, ...]}
            or an empty dict if no spans found.
        """
        spans = self._repo.get_trace_spans(trace_id)
        if not spans:
            log.warning("assemble_no_spans", trace_id=trace_id)
            return {}

        span_by_id: dict[str, dict] = {s["span_id"]: s for s in spans}

        # Build child map
        children_map: dict[str, list[str]] = {s["span_id"]: [] for s in spans}
        root_ids: list[str] = []

        for span in spans:
            pid = span.get("parent_span_id", "")
            if pid and pid in span_by_id:
                children_map[pid].append(span["span_id"])
            else:
                root_ids.append(span["span_id"])

        if not root_ids:
            # Fallback: pick earliest span as root
            root_ids = [spans[0]["span_id"]]

        def _build_node(span_id: str) -> dict:
            return {
                "span": span_by_id[span_id],
                "children": [
                    _build_node(child_id) for child_id in children_map[span_id]
                ],
            }

        if len(root_ids) == 1:
            return _build_node(root_ids[0])

        # Multiple roots — wrap in a synthetic root
        return {
            "span": {
                "trace_id": trace_id,
                "span_id": "__root__",
                "span_name": "__synthetic_root__",
            },
            "children": [_build_node(rid) for rid in root_ids],
        }

    def get_task_spans(self, trace_id: str) -> list[dict]:
        """Return only agent.task spans for the trace."""
        spans = self._repo.get_trace_spans(trace_id)
        return [s for s in spans if s.get("span_name") == "agent.task"]

    def get_tool_spans(self, trace_id: str) -> list[dict]:
        """Return only agent.tool_call spans for the trace."""
        spans = self._repo.get_trace_spans(trace_id)
        return [s for s in spans if s.get("span_name") == "agent.tool_call"]

    def get_handoff_spans(self, trace_id: str) -> list[dict]:
        """Return only agent.handoff spans for the trace."""
        spans = self._repo.get_trace_spans(trace_id)
        return [s for s in spans if s.get("span_name") == "agent.handoff"]

    def extract_eval_targets(
        self,
        trace_id: str,
        hint_spans: list[dict] | None = None,
    ) -> dict:
        """
        Return categorised spans for the eval pipeline.

        Queries ClickHouse for spans. If the result is empty (ClickHouse batch
        hasn't flushed yet), falls back to hint_spans from the OTLP payload.

        Keys: task_spans, tool_spans, handoff_spans, llm_spans, all_spans
        """
        all_spans = self._repo.get_trace_spans(trace_id)

        # Fallback: use in-flight spans if ClickHouse hasn't flushed yet
        if not all_spans and hint_spans:
            all_spans = [
                s for s in hint_spans
                if s.get("trace_id") == trace_id
            ]
            if all_spans:
                log.info(
                    "extract_eval_targets_used_hint",
                    trace_id=trace_id,
                    span_count=len(all_spans),
                )

        task_spans = [s for s in all_spans if s.get("span_name") == "agent.task"]
        tool_spans = [s for s in all_spans if s.get("span_name") == "agent.tool_call"]
        handoff_spans = [s for s in all_spans if s.get("span_name") == "agent.handoff"]
        # Auto-instrumented spans (opentelemetry-instrumentation-openai, LiteLLM auto, etc.)
        # These carry accurate token counts and are preferred when present.
        # call_llm is ADK's wrapper — excluded to avoid double-counting with its children.
        # agent.llm_call is the manual span from opt-demo runner.py — excluded when auto spans exist.
        # generate_content is ADK's LLM span (e.g. "generate_content openai/gpt-4o-mini").
        # openai.chat is injected by opentelemetry-instrumentation-openai as a child of
        # generate_content — same token data, so including both causes 2x inflation.
        # Only include openai.chat when no generate_content spans exist (non-ADK traces).
        _generate_content_spans = [
            s for s in all_spans
            if "generate_content" in s.get("span_name", "").lower()
        ]
        _auto_llm_spans = [
            s for s in all_spans
            if s.get("span_name") not in {"call_llm", "agent.llm_call"}
            and (
                "llm" in s.get("span_name", "").lower()
                or "generate_content" in s.get("span_name", "").lower()
                or (not _generate_content_spans and "chat" in s.get("span_name", "").lower())
            )
        ]
        _manual_llm_spans = [
            s for s in all_spans
            if s.get("span_name") == "agent.llm_call"
        ]
        # Prefer auto-instrumented; fall back to manual when no auto spans exist
        llm_spans = _auto_llm_spans if _auto_llm_spans else _manual_llm_spans

        # ---- Standards-based recognition (additive) ----
        # Everything above is ACP's own literal span-name matching, kept
        # unchanged so opt-demo / existing in-process integrations behave
        # exactly as before. This block additionally picks up spans that only
        # a standard dialect recognizes — OTel GenAI Semantic Conventions,
        # OpenInference, or OpenLLMetry — via ingestion.semconv_mapping. This
        # is what lets a raw trace from an unmodified external agent
        # framework (no ACP tracer, just a community auto-instrumentor
        # pointed at this collector) populate task/tool/handoff/llm targets.
        # Spans normalize_span() falls through to the acp_native dialect for
        # are skipped here — they're already covered by the literal checks
        # above; id-based sets make this purely a safety/no-op skip, not a
        # correctness requirement.
        task_ids = {s.get("span_id", "") for s in task_spans}
        tool_ids = {s.get("span_id", "") for s in tool_spans}
        handoff_ids = {s.get("span_id", "") for s in handoff_spans}
        llm_ids = {s.get("span_id", "") for s in llm_spans}

        # A framework that is BOTH manually instrumented (ACP-native agent.task
        # spans, e.g. opt-demo's runner.py) AND auto-instrumented (its own
        # native OTel GenAI export, e.g. ADK's "invoke_agent <agent>" span)
        # emits two nested representations of the SAME agent invocation, not
        # two separate ones — e.g. ADK's "invoke_agent translator" span sits
        # inside opt-demo's own "agent.task" span for that same translator
        # call. Additively recognizing the inner one as a second, distinct
        # task span splits one invocation across two prompt_eval rows: the
        # LLM call's tokens attribute to whichever task span is nearer (the
        # content-less inner one), while the outer span — which actually has
        # task.input/task.output — gets zero tokens and a bogus "unknown"-
        # agent sibling row appears alongside it. Only the outermost
        # task-level span per branch should count; a dialect-recognized
        # candidate nested under one already recognized (native or dialect)
        # is a redundant view of the same invocation, not a new one.
        _parent_map = {
            s.get("span_id", ""): s.get("parent_span_id", "")
            for s in all_spans if s.get("span_id")
        }

        def _has_task_ancestor(span_id: str, known_task_ids: set[str]) -> bool:
            seen: set[str] = set()
            current = _parent_map.get(span_id, "")
            while current and current not in seen:
                seen.add(current)
                if current in known_task_ids:
                    return True
                current = _parent_map.get(current, "")
            return False

        _dialect_task_candidate_ids = {
            span.get("span_id", "")
            for span in all_spans
            if span.get("span_id", "") not in task_ids
            and (norm := normalize_span(span)) is not None
            and norm["raw_dialect"] != "acp_native"
            and norm["kind"] == KIND_AGENT_TASK
        }
        _all_task_like_ids = task_ids | _dialect_task_candidate_ids

        # Same problem, one layer down: a single real LLM call is often
        # represented by a *chain* of nested spans at different instrumentation
        # layers — e.g. ADK's own "call_llm" wrapper -> its "generate_content
        # <model>" span -> opentelemetry-instrumentation-openai's "openai.chat"
        # child of that. The native construction above already picks exactly
        # one of these (prefers "generate_content", explicitly excludes
        # "call_llm" and — when generate_content exists — "openai.chat") to
        # avoid counting the same tokens 2-3x. The additive dialect probes
        # below have no knowledge of that exclusion list (dialect 3's generic
        # "has any gen_ai.* attribute" check matches all three layers), so
        # without a matching dedup here they silently re-add the very spans
        # the native logic deliberately excluded — inflating every token/cost
        # figure by however many redundant layers exist (3x for this chain).
        def _related_to_known_llm(span_id: str, known_llm_ids: set[str]) -> bool:
            seen: set[str] = set()
            current = _parent_map.get(span_id, "")
            while current and current not in seen:
                seen.add(current)
                if current in known_llm_ids:
                    return True  # candidate is a descendant of a known LLM span
                current = _parent_map.get(current, "")
            for llm_id in known_llm_ids:
                seen2: set[str] = set()
                current = _parent_map.get(llm_id, "")
                while current and current not in seen2:
                    if current == span_id:
                        return True  # candidate is an ancestor of a known LLM span
                    seen2.add(current)
                    current = _parent_map.get(current, "")
            return False

        for span in all_spans:
            span_id = span.get("span_id", "")
            norm = normalize_span(span)
            if norm is None or norm["raw_dialect"] == "acp_native":
                continue
            kind = norm["kind"]
            if kind == KIND_AGENT_TASK and span_id not in task_ids:
                if _has_task_ancestor(span_id, _all_task_like_ids):
                    continue  # nested under an already-recognized task span — same invocation
                task_spans.append(span)
                task_ids.add(span_id)
            elif kind == KIND_TOOL_CALL and span_id not in tool_ids:
                tool_spans.append(span)
                tool_ids.add(span_id)
            elif kind == KIND_HANDOFF and span_id not in handoff_ids:
                handoff_spans.append(span)
                handoff_ids.add(span_id)
            elif kind == KIND_LLM_CALL and span_id not in llm_ids:
                if _related_to_known_llm(span_id, llm_ids):
                    continue  # same underlying call as an already-recognized LLM span, different layer
                llm_spans.append(_with_canonical_llm_attrs(span, norm))
                llm_ids.add(span_id)

        return {
            "task_spans": task_spans,
            "tool_spans": tool_spans,
            "handoff_spans": handoff_spans,
            "llm_spans": llm_spans,
            "all_spans": all_spans,
        }
