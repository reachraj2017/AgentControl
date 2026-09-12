"""
Gateway ingest pipeline — generalizes ShadowEvalPipeline's proven pattern
(read gateway_call_log directly on a background loop) to ALL gateway calls,
routing them through the real 68-metric EvalPipeline instead of a 3-metric
side judge.

gateway_call_log (+ gateway_structural_events for handoff/tool_span/checkpoint
signals) is the durable queue. This pipeline polls it, groups rows into
traces, and either:
  - MERGE: an in-process trace already exists for this trace_id (ACP tracer,
    or a community OpenInference/OpenLLMetry auto-instrumentor already fed
    M1 directly) — attach gateway metadata only, never re-evaluate.
  - SYNTHESISE: no in-process trace exists — build a flat 2-level target set
    (conversation -> N calls, each with a nested llm_call child) and run it
    through EvalPipeline.run_from_targets(), the same code path the
    in-process span-tree path uses.

gateway_call_log itself is immutable; ingestion progress is tracked in the
gateway_call_eval_state sidecar table via Repository.mark_gateway_calls().
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

import structlog

log = structlog.get_logger(__name__)

_NS_PER_MS = 1_000_000

# How long to wait for an in-process trace to appear for a REAL propagated
# trace_id before assuming none is coming and synthesising from gateway_call_log
# alone. Must comfortably exceed the in-process trigger's own debounce window
# (EVAL_DEBOUNCE_SECONDS, main.py) plus its full LLM-judge evaluation cascade
# (routinely 30-70s+ for a multi-agent conversation) — otherwise this loop's
# 5s poll races that pipeline and, on losing, writes a premature partial
# evaluation under its own run_id before the correct complete one lands.
_INPROCESS_GRACE_SECONDS = 120


def _mk_id() -> str:
    return uuid.uuid4().hex


def _synth_trace_id(kind: str, value: str) -> str:
    """
    Deterministic 32-hex-char id for a group that has no real W3C trace_id
    (gateway-only, no traceparent propagated). Deterministic so that calls
    belonging to the same conversation/run arriving across separate polling
    cycles keep landing under the same eval trace_id — required for
    multi-turn metrics and for Prompt Analysis to group rows correctly.
    """
    return hashlib.sha256(f"gw:{kind}:{value}".encode()).hexdigest()[:32]


def _parse_json(raw: str | None) -> list | dict:
    if not raw:
        return []
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return []


def _extract_tool_calls(row: dict) -> list[dict]:
    """Pull tool calls + their results out of a gateway_call_log row's messages_json."""
    messages = _parse_json(row.get("messages_json"))
    if not isinstance(messages, list):
        messages = []

    results_by_id: dict[str, str] = {}
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "tool":
            tcid = m.get("tool_call_id", "")
            if tcid:
                results_by_id[tcid] = m.get("content", "") or ""

    tool_calls: list[dict] = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                tool_calls.append({
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments", ""),
                    "result": results_by_id.get(tc.get("id", ""), ""),
                })

    if not tool_calls:
        response_tool_calls = _parse_json(row.get("response_tool_calls_json"))
        if isinstance(response_tool_calls, list):
            for tc in response_tool_calls:
                fn = tc.get("function") or tc if isinstance(tc, dict) else {}
                tool_calls.append({
                    "name": fn.get("name", ""),
                    "args": fn.get("arguments", ""),
                    "result": "",
                })

    return tool_calls


def _as_dt(value) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)


class _GroupKey:
    """Identifies which rows belong to the same logical trace."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: str, value: str) -> None:
        self.kind = kind
        self.value = value

    def __hash__(self) -> int:
        return hash((self.kind, self.value))

    def __eq__(self, other) -> bool:
        return isinstance(other, _GroupKey) and self.kind == other.kind and self.value == other.value

    @property
    def eval_trace_id(self) -> str:
        # A real propagated trace_id is used as-is (hex32); everything else
        # (conversation/run/singleton-call grouping) gets a deterministic
        # synthetic id so repeated polling cycles converge on the same trace.
        if self.kind == "trace":
            return self.value
        return _synth_trace_id(self.kind, self.value)


def _group_key(row: dict) -> _GroupKey:
    """
    Grouping priority per design doc §5.3 / §3: trace_id > conversation_id >
    run_id > singleton call_id.
    """
    if row.get("trace_id"):
        return _GroupKey("trace", row["trace_id"])
    if row.get("conversation_id"):
        return _GroupKey("conv", row["conversation_id"])
    if row.get("run_id"):
        return _GroupKey("run", row["run_id"])
    return _GroupKey("call", row["call_id"])


class GatewayIngestPipeline:
    """Reads gateway_call_log (+ gateway_structural_events) and feeds EvalPipeline."""

    def __init__(self, repository, eval_pipeline) -> None:
        self._repo = repository
        self._eval = eval_pipeline

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_batch(self, batch_size: int = 50) -> int:
        rows = self._repo.get_pending_gateway_calls(batch_size)
        if not rows:
            return 0

        groups: dict[_GroupKey, list[dict]] = defaultdict(list)
        for row in rows:
            groups[_group_key(row)].append(row)

        conv_ids = sorted({r.get("conversation_id", "") for r in rows if r.get("conversation_id")})
        run_ids = sorted({r.get("run_id", "") for r in rows if r.get("run_id")})
        structural_events = self._repo.get_pending_structural_events(conv_ids, run_ids)
        structural_by_conv: dict[str, list[dict]] = defaultdict(list)
        structural_by_run: dict[str, list[dict]] = defaultdict(list)
        for ev in structural_events:
            if ev.get("conversation_id"):
                structural_by_conv[ev["conversation_id"]].append(ev)
            if ev.get("run_id"):
                structural_by_run[ev["run_id"]].append(ev)

        for key, group_rows in groups.items():
            call_ids = [r["call_id"] for r in group_rows if r.get("call_id")]
            try:
                if key.kind == "trace":
                    if self._repo.trace_has_inprocess_spans(key.value):
                        # In-process tracer (ACP SDK, or a community OpenInference /
                        # OpenLLMetry auto-instrumentor) already produced this
                        # trace and it has already been (or will be) evaluated via
                        # the normal OTLP trigger path. Attach metadata only —
                        # never call run_from_targets here, to avoid a double
                        # score for the same conversation.
                        self._repo.mark_gateway_calls(call_ids, "done", key.eval_trace_id)
                        continue

                    # No in-process spans YET is not the same as "none are
                    # coming". A real W3C trace_id only exists on these rows
                    # because something propagated it — almost always an
                    # in-process tracer that WILL eventually write spans for
                    # it (e.g. opt-demo: every call is both gateway-routed
                    # AND in-process-traced). That tracer has its own
                    # debounce plus tens of seconds of sequential LLM-judge
                    # calls before it writes anything, while this loop polls
                    # every 5s — racing it and losing produces a premature,
                    # partial SYNTHESISE pass with its own run_id, bypassing
                    # the debounced trigger entirely and leaving a permanent
                    # duplicate/incomplete row behind once the real
                    # in-process pass finishes later and does it correctly.
                    # Give the in-process path real time to land before
                    # ever falling back to synthesising: leave these rows
                    # pending (do not mark done, do not synthesise) and
                    # re-check on a later poll, up to a bounded grace period —
                    # after which we do assume gateway-only (e.g. a
                    # propagated header with no tracer actually listening).
                    oldest = min(_as_dt(r.get("created_at")) for r in group_rows)
                    if oldest.tzinfo is None:
                        # clickhouse_driver returns naive datetimes for
                        # DateTime64 columns; every writer in this stack
                        # populates them in UTC (see db.py's log_call), so
                        # treat a naive value as UTC rather than crash on
                        # naive-vs-aware subtraction below.
                        oldest = oldest.replace(tzinfo=timezone.utc)
                    age = (datetime.now(timezone.utc) - oldest).total_seconds()
                    if age < _INPROCESS_GRACE_SECONDS:
                        log.debug(
                            "gateway_ingest_awaiting_inprocess_trace",
                            trace_id=key.value, age_seconds=round(age, 1),
                        )
                        continue

                events = self._events_for_group(group_rows, structural_by_conv, structural_by_run)
                targets = self._synthesise_targets(group_rows, events)
                if not targets["task_spans"]:
                    self._repo.mark_gateway_calls(call_ids, "skipped", key.eval_trace_id)
                    continue

                run_id = self._resolve_run_id(group_rows)
                self._eval.run_from_targets(
                    targets,
                    trace_id=key.eval_trace_id,
                    run_id=run_id,
                    mode="online",
                )
                self._repo.mark_gateway_calls(call_ids, "done", key.eval_trace_id)
            except Exception as exc:
                log.error("gateway_ingest_group_failed", group_kind=key.kind, error=str(exc))
                self._repo.mark_gateway_calls(call_ids, "error", "")

        return len(rows)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _events_for_group(
        self,
        group_rows: list[dict],
        structural_by_conv: dict[str, list[dict]],
        structural_by_run: dict[str, list[dict]],
    ) -> list[dict]:
        seen_ids: set[str] = set()
        events: list[dict] = []
        for row in group_rows:
            for ev in structural_by_conv.get(row.get("conversation_id", ""), []):
                if ev["event_id"] not in seen_ids:
                    events.append(ev)
                    seen_ids.add(ev["event_id"])
            for ev in structural_by_run.get(row.get("run_id", ""), []):
                if ev["event_id"] not in seen_ids:
                    events.append(ev)
                    seen_ids.add(ev["event_id"])
        return events

    def _resolve_run_id(self, group_rows: list[dict]) -> str:
        for row in group_rows:
            if row.get("run_id"):
                return row["run_id"]
        # No caller-supplied run_id — fall back to the eval-runner's default
        # run, same as the in-process trigger path does for un-tagged spans.
        try:
            runs = self._repo.get_runs(limit=10)
            for run in runs:
                if run.get("name") == "default":
                    return str(run["run_id"])
            return self._repo.create_run(name="default")
        except Exception:
            return str(uuid.uuid4())

    def _synthesise_targets(self, group_rows: list[dict], structural_events: list[dict]) -> dict:
        """Build a flat conversation -> N calls target set from raw gateway rows."""
        rows_sorted = sorted(group_rows, key=lambda r: _as_dt(r.get("started_at") or r.get("created_at")))

        explicit_handoffs = [e for e in structural_events if e.get("call_type") == "handoff"]
        explicit_tool_spans = [e for e in structural_events if e.get("call_type") == "tool_span"]

        task_spans: list[dict] = []
        llm_spans: list[dict] = []
        tool_spans: list[dict] = []
        handoff_spans: list[dict] = []
        all_spans: list[dict] = []

        root_id: Optional[str] = None
        prev_role: Optional[str] = None

        for i, row in enumerate(rows_sorted):
            task_id = _mk_id()
            if i == 0:
                root_id = task_id
            agent_role = row.get("agent_role", "") or "unknown"
            latency_ms = int(row.get("latency_ms") or 0)
            status_error = row.get("status") == "error"

            task_span = {
                "span_id": task_id,
                "parent_span_id": "" if i == 0 else root_id,
                "span_name": "agent.task",
                "status_code": "STATUS_CODE_ERROR" if status_error else "STATUS_CODE_OK",
                "duration_ns": latency_ms * _NS_PER_MS,
                "attributes": {
                    "agent.role": agent_role,
                    "agent.id": agent_role,
                    "task.input": row.get("prompt_text", "") or "",
                    "task.output": row.get("response_text", "") or "",
                    "run.id": row.get("run_id", "") or "",
                    "conversation.id": row.get("conversation_id", "") or "",
                    "trace.source": "gateway",
                },
            }
            task_spans.append(task_span)

            llm_span = {
                "span_id": _mk_id(),
                "parent_span_id": task_id,
                "span_name": "llm_call",
                "status_code": "STATUS_CODE_ERROR" if status_error else "STATUS_CODE_OK",
                "duration_ns": latency_ms * _NS_PER_MS,
                "attributes": {
                    "gen_ai.request.model": row.get("model_used", "") or "",
                    "gen_ai.usage.input_tokens": int(row.get("tokens_in") or 0),
                    "gen_ai.usage.output_tokens": int(row.get("tokens_out") or 0),
                    "gen_ai.prompt": row.get("prompt_text", "") or "",
                    "gen_ai.completion": row.get("response_text", "") or "",
                },
            }
            llm_spans.append(llm_span)

            for tc in _extract_tool_calls(row):
                if not tc.get("name"):
                    continue
                tool_spans.append({
                    "span_id": _mk_id(),
                    "parent_span_id": task_id,
                    "span_name": "agent.tool_call",
                    "status_code": "STATUS_CODE_OK",
                    "duration_ns": 0,
                    "attributes": {
                        "tool.name": tc["name"],
                        "tool.input": tc.get("args", ""),
                        "tool.output": tc.get("result", ""),
                        "agent.id": agent_role,
                    },
                })

            # Role-change handoff inference — only when no explicit handoff
            # events exist for this group (explicit signals from
            # /v1/handoff take precedence over the heuristic).
            if not explicit_handoffs and prev_role and prev_role != agent_role:
                handoff_spans.append({
                    "span_id": _mk_id(),
                    "parent_span_id": root_id,
                    "span_name": "agent.handoff",
                    "status_code": "STATUS_CODE_OK",
                    "duration_ns": 0,
                    "attributes": {
                        "handoff.from": prev_role,
                        "handoff.to": agent_role,
                        "handoff.inferred": True,
                    },
                })
            prev_role = agent_role

            all_spans.append(task_span)
            all_spans.append(llm_span)

        all_spans.extend(tool_spans)
        all_spans.extend(handoff_spans)

        # Explicit handoff events (from /v1/handoff) — attach under the
        # root task span, one per event, taking
        # precedence over the role-change heuristic above.
        for ev in explicit_handoffs:
            payload = _parse_json(ev.get("payload_json"))
            if not isinstance(payload, dict):
                payload = {}
            span = {
                "span_id": _mk_id(),
                "parent_span_id": root_id or "",
                "span_name": "agent.handoff",
                "status_code": "STATUS_CODE_OK",
                "duration_ns": 0,
                "attributes": {
                    "handoff.from": payload.get("from_agent", ""),
                    "handoff.to": payload.get("to_agent", ""),
                    "handoff.context_summary": payload.get("context_summary", ""),
                    "handoff.inferred": False,
                },
            }
            handoff_spans.append(span)
            all_spans.append(span)

        # Explicit tool-span events (from /v1/tool-span) — attached to the
        # nearest preceding task span for the same agent_role, falling back
        # to the root task span if none matches.
        role_to_task_id = {t["attributes"]["agent.role"]: t["span_id"] for t in task_spans}
        for ev in explicit_tool_spans:
            payload = _parse_json(ev.get("payload_json"))
            if not isinstance(payload, dict):
                payload = {}
            parent = role_to_task_id.get(ev.get("agent_role", ""), root_id or "")
            span = {
                "span_id": _mk_id(),
                "parent_span_id": parent,
                "span_name": "agent.tool_call",
                "status_code": "STATUS_CODE_ERROR" if payload.get("status") == "error" else "STATUS_CODE_OK",
                "duration_ns": int(payload.get("latency_ms") or 0) * _NS_PER_MS,
                "attributes": {
                    "tool.name": payload.get("tool_name", ""),
                    "tool.input": payload.get("input", ""),
                    "tool.output": payload.get("output", ""),
                    "agent.id": ev.get("agent_role", ""),
                },
            }
            tool_spans.append(span)
            all_spans.append(span)

        return {
            "task_spans": task_spans,
            "tool_spans": tool_spans,
            "handoff_spans": handoff_spans,
            "llm_spans": llm_spans,
            "all_spans": all_spans,
        }
