"""
Trigger logic: detects completed agent.task spans and fires the eval pipeline.
"""

import asyncio
import re
from pathlib import Path
from typing import Optional

import structlog
import yaml

from ingestion.semconv_mapping import is_agent_task_span, register_task_span_names

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

log = structlog.get_logger(__name__)

_DEFAULT_RUN_NAME = "default"

# Same config file EvalPipeline reads (config/evaluator_config.yaml).
_CONFIG_PATH = Path(__file__).parent.parent / "config" / "evaluator_config.yaml"


def _load_extra_task_span_names() -> list[str]:
    """Read ingestion.task_span_names from evaluator_config.yaml, if present."""
    try:
        with open(_CONFIG_PATH, "r") as f:
            cfg = yaml.safe_load(f) or {}
        return cfg.get("ingestion", {}).get("task_span_names", []) or []
    except FileNotFoundError:
        return []
    except yaml.YAMLError as exc:
        log.warning("trigger_config_parse_error", error=str(exc))
        return []


# Extend the acp_native dialect's recognized task-span names at import time —
# operators add framework-specific names via config, no code change needed.
register_task_span_names(_load_extra_task_span_names())


class EvalTrigger:
    """Decides when to fire the eval pipeline and which run to attribute scores to."""

    # Status codes that indicate a span completed successfully
    _SUCCESS_CODES = {
        "STATUS_CODE_OK",
        "STATUS_CODE_UNSET",   # Unset is treated as non-error in OTel semantics
    }

    def is_completed_task(self, span: dict) -> bool:
        """
        Return True if this span represents a completed agent task boundary.

        Recognizes ACP's own 'agent.task' span AND the equivalent shape in
        OTel GenAI Semantic Conventions (invoke_agent), OpenInference
        (AGENT/CHAIN span.kind), and OpenLLMetry (workflow/task/agent
        span.kind) via ingestion.semconv_mapping.is_agent_task_span — so a raw
        trace from an unmodified external framework using any of those
        standard instrumentation libraries triggers evaluation too, not just
        ACP's own tracer. A span is considered completed when it's recognized
        as an agent-task span and its status code is not STATUS_CODE_ERROR.
        """
        if not is_agent_task_span(span):
            return False
        status = span.get("status_code", "STATUS_CODE_UNSET")
        return status != "STATUS_CODE_ERROR"

    def get_run_id(self, span: dict) -> Optional[str]:
        """
        Extract run.id from span attributes.

        Returns a valid UUID string if present, or None.
        Short/non-UUID run.id values (e.g. 8-char truncated IDs from the demo)
        are treated as absent so the default run is used instead.
        """
        attrs = span.get("attributes", {})
        value = attrs.get("run.id") or None
        if value and not _UUID_RE.match(value):
            return None
        return value

    async def trigger_evaluation(
        self,
        span: dict,
        pipeline,
        repository,
        hint_spans: list[dict] | None = None,
    ) -> None:
        """
        Determine the run_id for a span and fire the eval pipeline.

        If the span carries a run.id attribute it is used directly.
        Otherwise the repository is queried for the most recent run
        named 'default'; if none exists one is created.

        hint_spans: spans received in the same OTLP payload (used as fallback
        if ClickHouse hasn't flushed the batch yet).
        """
        trace_id = span.get("trace_id", "")
        if not trace_id:
            log.warning("trigger_no_trace_id", span_id=span.get("span_id"))
            return

        run_id = self.get_run_id(span)

        if not run_id:
            run_id = self._get_or_create_default_run(repository)

        log.info(
            "eval_triggered",
            trace_id=trace_id,
            run_id=run_id,
            span_id=span.get("span_id"),
        )

        try:
            # pipeline.run() is a plain synchronous call — a sequential cascade
            # of LLM-judge HTTP calls that routinely takes 15-90+ seconds. Called
            # directly inside this async function it blocks the ENTIRE process's
            # single-threaded event loop for that whole duration: no new OTLP
            # ingestion, no health checks, and no other background loop (e.g.
            # main.py's conversation-idle loop) gets to run either. Offload it
            # to a worker thread, matching the pattern _shadow_eval_loop and
            # _gateway_ingest_loop already use correctly for the same reason.
            await asyncio.to_thread(
                pipeline.run,
                trace_id=trace_id,
                run_id=run_id,
                mode="online",
                hint_spans=hint_spans,
            )
        except Exception as exc:
            log.error(
                "eval_pipeline_error",
                trace_id=trace_id,
                run_id=run_id,
                error=str(exc),
            )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _get_or_create_default_run(self, repository) -> str:
        """
        Return the run_id of the most recent 'default' run, creating
        one if none exists.
        """
        try:
            runs = repository.get_runs(limit=10)
            for run in runs:
                if run.get("name") == _DEFAULT_RUN_NAME:
                    return str(run["run_id"])
        except Exception as exc:
            log.warning("trigger_get_runs_failed", error=str(exc))

        # Create a new default run
        try:
            run_id = repository.create_run(name=_DEFAULT_RUN_NAME)
            log.info("default_run_created", run_id=run_id)
            return run_id
        except Exception as exc:
            log.error("trigger_create_run_failed", error=str(exc))
            # Return a dummy UUID so the pipeline can still attempt to run
            import uuid
            return str(uuid.uuid4())
