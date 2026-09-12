"""
Eval Runner - Main FastAPI Application

Receives OTLP trace data, stores spans, triggers evaluation pipeline
on completed agent.task spans.
"""

import asyncio
import os
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import Any, Optional

import structlog
from fastapi import BackgroundTasks, FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from db.repository import Repository
from ingestion.span_receiver import SpanReceiver
from ingestion.trace_assembler import TraceAssembler
from pipeline.eval_pipeline import EvalPipeline
from pipeline.conversation_eval import ConversationEvalPipeline
from pipeline.gateway_ingest_pipeline import GatewayIngestPipeline
from pipeline.shadow_eval_pipeline import ShadowEvalPipeline
from pipeline.trigger import EvalTrigger
from regression.comparator import RegressionComparator
from tracing.eval_tracer import setup_eval_tracing

# ------------------------------------------------------------------
# Conversation tracker: conversation_id → {last_seen, run_id}
# Conversations idle for > CONV_IDLE_SECONDS trigger multi-turn eval.
# ------------------------------------------------------------------
_conversation_tracker: dict[str, dict] = {}
CONV_IDLE_SECONDS = 60

# ------------------------------------------------------------------
# Eval debounce: trace_id → the currently-scheduled evaluation task.
#
# A multi-agent trace (opt-demo's orchestrator -> searcher -> translator,
# say) completes its sub-agents' OTel spans incrementally over time, each
# export landing in its own OTLP batch as that sub-agent finishes. The
# previous design fired on the FIRST completed agent-task span seen for a
# trace and then suppressed any further firing for a fixed 30s cooldown —
# which works only if the whole conversation finishes within 30s of its
# first agent completing. A tool-call round-trip or a slower LLM response
# routinely pushes a real multi-hop conversation past that, so the cooldown
# expired mid-conversation and a LATER sub-agent's completion fired a SECOND
# (and sometimes third) independent full-trace evaluation — each one
# capturing whatever partial state existed at that moment and writing its
# own prompt_eval/eval_scores rows, none of which get cleaned up. That's
# what produced 5 prompt_eval rows for a 3-agent trace instead of 3.
#
# Fixed as a genuine debounce instead of a cooldown: every new completed
# agent-task span for a trace CANCELS whatever evaluation was already
# scheduled for it and reschedules a fresh one. The pipeline only actually
# runs once nothing new has completed for EVAL_DEBOUNCE_SECONDS — i.e. once
# the whole conversation, however many hops it took, has gone quiet — so
# exactly one evaluation pass happens per conversation regardless of how
# long it runs or how many OTLP batches its spans arrive across.
# ------------------------------------------------------------------
_pending_eval: dict[str, asyncio.Task] = {}
EVAL_DEBOUNCE_SECONDS = 10   # also doubles as the existing ClickHouse-flush wait

# In-progress lock: trace_ids currently being evaluated.
# Prevents concurrent online + offline pipeline runs for the same trace,
# which causes duplicate prompt_eval rows due to ClickHouse TOCTOU races.
_pipeline_in_progress: set[str] = set()

# ------------------------------------------------------------------
# Logging
# ------------------------------------------------------------------
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.JSONRenderer(),
    ]
)
log = structlog.get_logger(__name__)

# ------------------------------------------------------------------
# Application-level singletons (initialised at startup)
# ------------------------------------------------------------------
_repo: Optional[Repository] = None
_span_receiver: Optional[SpanReceiver] = None
_trace_assembler: Optional[TraceAssembler] = None
_pipeline: Optional[EvalPipeline] = None
_conv_pipeline: Optional[ConversationEvalPipeline] = None
_shadow_pipeline: Optional[ShadowEvalPipeline] = None
_gateway_ingest_pipeline: Optional[GatewayIngestPipeline] = None
_trigger: Optional[EvalTrigger] = None
_comparator: Optional[RegressionComparator] = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise shared singletons on startup."""
    global _repo, _span_receiver, _trace_assembler, _pipeline, _conv_pipeline, _shadow_pipeline, _gateway_ingest_pipeline, _trigger, _comparator

    config_path = os.getenv(
        "EVALUATOR_CONFIG_PATH",
        os.path.join(os.path.dirname(__file__), "config", "evaluator_config.yaml"),
    )

    setup_eval_tracing()

    _repo = Repository()
    _span_receiver = SpanReceiver()
    _trace_assembler = TraceAssembler(repository=_repo)
    _pipeline = EvalPipeline(
        repository=_repo,
        trace_assembler=_trace_assembler,
        evaluator_config_path=config_path,
    )
    _conv_pipeline = ConversationEvalPipeline(repository=_repo)
    _shadow_pipeline = ShadowEvalPipeline(repository=_repo)
    _gateway_ingest_pipeline = GatewayIngestPipeline(repository=_repo, eval_pipeline=_pipeline)
    _trigger = EvalTrigger()
    _comparator = RegressionComparator()

    # Start background task that fires conversation eval on idle conversations
    asyncio.create_task(_conversation_eval_loop())
    # Start background task that scores unscored shadow calls from the gateway
    asyncio.create_task(_shadow_eval_loop())
    # Start background task that runs gateway-only (no ACP tracer) calls
    # through the real 68-metric pipeline
    asyncio.create_task(_gateway_ingest_loop())

    log.info("eval_runner_started")
    yield
    log.info("eval_runner_stopped")


app = FastAPI(
    title="Eval Runner",
    description="Multi-agent AI evaluation platform – OTLP ingestion and scoring service",
    version="1.0.0",
    lifespan=lifespan,
)


# ------------------------------------------------------------------
# Dependency helpers
# ------------------------------------------------------------------

def get_repo() -> Repository:
    if _repo is None:
        raise RuntimeError("Repository not initialised")
    return _repo


def get_pipeline() -> EvalPipeline:
    if _pipeline is None:
        raise RuntimeError("EvalPipeline not initialised")
    return _pipeline


def get_trigger() -> EvalTrigger:
    if _trigger is None:
        raise RuntimeError("EvalTrigger not initialised")
    return _trigger


def get_comparator() -> RegressionComparator:
    if _comparator is None:
        raise RuntimeError("RegressionComparator not initialised")
    return _comparator


# ------------------------------------------------------------------
# Pydantic request/response models
# ------------------------------------------------------------------

class CreateRunRequest(BaseModel):
    name: str
    suite: str = ""
    agent_version: str = ""
    metadata: dict = {}


class CreateRunResponse(BaseModel):
    run_id: str
    name: str


class EvaluateRunRequest(BaseModel):
    mode: str = "offline"   # "offline" or "online"


# ------------------------------------------------------------------
# Routes
# ------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    """Liveness probe."""
    return {"status": "ok", "service": "eval-runner"}


# ---- OTLP ingestion ------------------------------------------------

@app.post("/v1/traces", status_code=200)
async def receive_traces(
    request: Request,
    background_tasks: BackgroundTasks,
) -> Response:
    """
    Receive OTLP trace data (JSON or protobuf).

    Returns 200 immediately so the OTel Collector never times out waiting.
    All span parsing, persistence, and eval triggering happen in background.
    """
    if _span_receiver is None:
        raise HTTPException(status_code=503, detail="Service not ready")

    body = await request.body()
    content_type = request.headers.get("content-type", "application/json")

    # ACK immediately — collector moves on, no timeout risk
    background_tasks.add_task(_process_spans_background, body, content_type)

    return Response(content='{"partialSuccess":{}}', media_type="application/json")


async def _process_spans_background(body: bytes, content_type: str) -> None:
    """Parse, persist, and optionally evaluate spans — runs after the HTTP response is sent."""
    receiver = _span_receiver
    if receiver is None:
        return

    spans = receiver.parse_otlp_http_body(body, content_type)
    if not spans:
        log.warning("receive_traces_no_spans_parsed")
        return

    repo = get_repo()
    try:
        repo.save_spans_batch(spans)
    except Exception as exc:
        log.error("receive_traces_save_failed", error=str(exc))
        return  # don't trigger eval if save failed

    trigger = get_trigger()
    pipeline = get_pipeline()
    completed_task_spans = [s for s in spans if trigger.is_completed_task(s)]

    log.info(
        "traces_received",
        total_spans=len(spans),
        eval_queued=len(completed_task_spans),
    )

    now = time.time()
    # Debounce: one completed task span per trace_id per batch is enough to
    # (re)schedule. A dual-instrumented call (opt-demo's manual ACP-native
    # "agent.task" span alongside ADK's own native "invoke_agent <agent>"
    # span for that same call) can put MULTIPLE recognized task-shaped spans
    # for the SAME trace_id in one batch. Which one "wins" the dedup matters: only the
    # ACP-native shape reliably carries the bare "conversation.id"/"run.id"
    # attribute keys this code and get_run_id() read — a dialect-recognized
    # span like "invoke_agent translator" carries the namespaced
    # "gen_ai.conversation.id" instead and has no run-id equivalent at all.
    # Picking whichever span happened to arrive first (the previous
    # behavior) silently broke conversation tracking whenever the native
    # span didn't win the race, AND could misattribute the eval to the
    # generic "default" run instead of the real one. Explicitly prefer the
    # native span per trace_id; fall back to whatever was recognized only
    # when no native span exists in the batch (a framework-native-only
    # integration with no ACP tracer at all, where there's nothing to
    # prefer).
    by_trace: dict[str, list[dict]] = defaultdict(list)
    for span in completed_task_spans:
        trace_id = span.get("trace_id", "")
        if trace_id:
            by_trace[trace_id].append(span)

    for trace_id, group in by_trace.items():
        span = next((s for s in group if s.get("span_name") == "agent.task"), group[0])

        pending = _pending_eval.get(trace_id)
        if pending is not None and not pending.done():
            pending.cancel()
            log.debug("eval_debounce_reset", trace_id=trace_id)

        _pending_eval[trace_id] = asyncio.create_task(
            _run_eval_in_background(span, pipeline, repo, trigger, spans)
        )

        # Track conversation turns for multi-turn eval
        attrs = span.get("attributes") or {}
        conv_id = attrs.get("conversation.id") or attrs.get("gen_ai.conversation.id") or ""
        if conv_id:
            run_id = trigger.get_run_id(span) or ""
            _conversation_tracker[conv_id] = {
                "last_seen": now,
                "run_id":    run_id,
            }


async def _conversation_eval_loop() -> None:
    """Periodic task: fire conversation eval for conversations idle > CONV_IDLE_SECONDS."""
    while True:
        await asyncio.sleep(30)  # check every 30s
        now = time.time()
        log.debug(
            "conversation_eval_loop_tick",
            tracked=len(_conversation_tracker),
            entries={k: round(now - v["last_seen"], 1) for k, v in list(_conversation_tracker.items())},
        )
        ready = [
            (conv_id, meta)
            for conv_id, meta in list(_conversation_tracker.items())
            if now - meta["last_seen"] >= CONV_IDLE_SECONDS
        ]
        for conv_id, meta in ready:
            del _conversation_tracker[conv_id]
            asyncio.create_task(_run_conversation_eval(conv_id, meta["run_id"]))


async def _shadow_eval_loop() -> None:
    """Periodic task: score unscored shadow calls from gateway_call_log."""
    await asyncio.sleep(30)  # let startup finish before first run
    while True:
        try:
            if _shadow_pipeline is not None:
                count = await asyncio.to_thread(_shadow_pipeline.run_batch)
                if count:
                    log.info("shadow_eval_scored", count=count)
        except Exception as exc:
            log.warning("shadow_eval_loop_error", error=str(exc))
        await asyncio.sleep(60)


async def _gateway_ingest_loop() -> None:
    """Periodic task: run pending gateway_call_log rows through the real EvalPipeline."""
    await asyncio.sleep(15)  # let startup finish before first run
    while True:
        try:
            if _gateway_ingest_pipeline is not None:
                count = await asyncio.to_thread(_gateway_ingest_pipeline.run_batch)
                if count:
                    log.info("gateway_ingest_processed", count=count)
        except Exception as exc:
            log.warning("gateway_ingest_loop_error", error=str(exc))
        await asyncio.sleep(5)


async def _run_conversation_eval(conversation_id: str, run_id: str) -> None:
    """Run ConversationEvalPipeline for a completed conversation."""
    if _conv_pipeline is None or _repo is None:
        return
    # Resolve run_id if blank
    if not run_id:
        try:
            runs = _repo.get_runs(limit=5)
            run_id = str(runs[0]["run_id"]) if runs else str(__import__("uuid").uuid4())
        except Exception:
            run_id = str(__import__("uuid").uuid4())
    try:
        log.info("conversation_eval_triggered", conversation_id=conversation_id, run_id=run_id)
        # Offload the synchronous pipeline run — see the matching comment in
        # pipeline/trigger.py's trigger_evaluation(): called directly this
        # blocks the whole event loop, including this very loop's own
        # asyncio.sleep(30) in _conversation_eval_loop, for the run's full
        # duration.
        await asyncio.to_thread(
            _conv_pipeline.run, conversation_id=conversation_id, run_id=run_id
        )
    except Exception as exc:
        log.error("conversation_eval_failed", conversation_id=conversation_id, error=str(exc))


async def _run_eval_in_background(
    span: dict,
    pipeline: EvalPipeline,
    repo: Repository,
    trigger: EvalTrigger,
    all_spans: list[dict] | None = None,
) -> None:
    """Debounced background task: waits, then evaluates — unless superseded.

    The sleep is both the pre-existing "give ClickHouse's batch window time
    to flush" wait AND the debounce window: if another completed agent-task
    span for this trace_id arrives before it elapses, the caller cancels
    this task and schedules a fresh one, so only the LAST one in a burst
    ever reaches trigger_evaluation. CancelledError is not caught below (it
    is not an Exception subclass) — it propagates normally, so a superseded
    run does not log as an error.
    """
    trace_id = span.get("trace_id", "")
    try:
        await asyncio.sleep(EVAL_DEBOUNCE_SECONDS)

        if trace_id in _pipeline_in_progress:
            log.debug("eval_pipeline_already_running", trace_id=trace_id)
            return
        _pipeline_in_progress.add(trace_id)
        try:
            await trigger.trigger_evaluation(span, pipeline, repo, hint_spans=all_spans)
            # Governance evaluation is handled by the standalone governance-service
            # via its span watcher (polls ClickHouse every 30s for unchecked traces).
        except Exception as exc:
            log.error(
                "background_eval_failed",
                trace_id=trace_id,
                error=str(exc),
            )
        finally:
            _pipeline_in_progress.discard(trace_id)
    finally:
        # Only clear the pending-eval slot if it's still THIS task — a newer
        # task may have already replaced it (e.g. this one was cancelled and
        # the replacement is now the one sleeping), and clearing then would
        # wrongly make the replacement look un-tracked.
        if _pending_eval.get(trace_id) is asyncio.current_task():
            del _pending_eval[trace_id]


# ---- Eval Runs -----------------------------------------------------

@app.get("/runs")
async def list_runs(limit: int = 50) -> list[dict]:
    """List recent eval runs."""
    try:
        return get_repo().get_runs(limit=limit)
    except Exception as exc:
        log.error("list_runs_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/runs", status_code=201)
async def create_run(body: CreateRunRequest) -> CreateRunResponse:
    """Create a new eval run and return its run_id."""
    try:
        run_id = get_repo().create_run(
            name=body.name,
            suite=body.suite,
            agent_version=body.agent_version,
            metadata=body.metadata,
        )
        return CreateRunResponse(run_id=run_id, name=body.name)
    except Exception as exc:
        log.error("create_run_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.put("/runs/{run_id}/baseline", status_code=200)
async def set_baseline(run_id: str) -> dict:
    """Promote a run to baseline."""
    try:
        get_repo().set_baseline(run_id)
        return {"run_id": run_id, "is_baseline": True}
    except Exception as exc:
        log.error("set_baseline_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/runs/{run_id}/evaluate", status_code=202)
async def trigger_run_evaluation(
    run_id: str,
    body: EvaluateRunRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Trigger offline evaluation for all traces in a run.

    Evaluation runs in the background; returns 202 Accepted immediately.
    """
    repo = get_repo()
    pipeline = get_pipeline()

    traces = repo.get_traces_for_run(run_id)
    if not traces:
        raise HTTPException(
            status_code=404,
            detail=f"No traces found for run {run_id}",
        )

    for trace_row in traces:
        trace_id = trace_row["trace_id"]
        background_tasks.add_task(
            _evaluate_trace_background,
            trace_id,
            run_id,
            body.mode,
            pipeline,
        )

    log.info("run_evaluation_queued", run_id=run_id, trace_count=len(traces))
    return {
        "run_id": run_id,
        "queued_traces": len(traces),
        "mode": body.mode,
        "status": "queued",
    }


async def _evaluate_trace_background(
    trace_id: str,
    run_id: str,
    mode: str,
    pipeline: EvalPipeline,
) -> None:
    if trace_id in _pipeline_in_progress:
        log.debug("offline_eval_skipped_in_progress", trace_id=trace_id)
        return
    _pipeline_in_progress.add(trace_id)
    try:
        # See the matching comment in pipeline/trigger.py's trigger_evaluation()
        # — a bare pipeline.run() call here blocks the whole process, including
        # live OTLP ingestion, for this run's full duration.
        await asyncio.to_thread(pipeline.run, trace_id=trace_id, run_id=run_id, mode=mode)
    except Exception as exc:
        log.error(
            "offline_eval_failed", trace_id=trace_id, run_id=run_id, error=str(exc)
        )
    finally:
        _pipeline_in_progress.discard(trace_id)


@app.get("/runs/{run_id}/scores")
async def get_run_scores(run_id: str) -> list[dict]:
    """Return per-metric aggregated scores for a run."""
    try:
        return get_repo().get_run_scores(run_id)
    except Exception as exc:
        log.error("get_run_scores_failed", run_id=run_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/runs/{run_id}/regression")
async def get_run_regression(run_id: str) -> dict:
    """
    Compare a run's scores against the current baseline.

    Returns per-metric deltas and a regression summary.
    """
    repo = get_repo()
    comparator = get_comparator()

    baseline = repo.get_baseline_run()
    if baseline is None:
        raise HTTPException(
            status_code=404,
            detail="No baseline run set. Use PUT /runs/{run_id}/baseline first.",
        )

    baseline_run_id = str(baseline["run_id"])
    if baseline_run_id == run_id:
        raise HTTPException(
            status_code=400,
            detail="Cannot compare a run against itself as baseline",
        )

    comparisons = comparator.compare(run_id, baseline_run_id, repo)
    summary = comparator.get_regression_summary(comparisons)

    return {
        "run_id": run_id,
        "baseline_run_id": baseline_run_id,
        "comparisons": comparisons,
        "summary": summary,
    }


# ---- Trace scores --------------------------------------------------

@app.get("/traces/{trace_id}/scores")
async def get_trace_scores(trace_id: str) -> list[dict]:
    """Return all eval scores for a specific trace."""
    try:
        return get_repo().get_scores_for_trace(trace_id)
    except Exception as exc:
        log.error("get_trace_scores_failed", trace_id=trace_id, error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Benchmark CRUD ------------------------------------------------

class CreateBenchmarkRequest(BaseModel):
    name: str
    task_input: str
    suite: str = "unit"
    difficulty: str = "medium"
    expected_output: str = ""
    rubric: str = ""
    tags: str = ""
    dataset_version: str = "v1"

class CreateBenchmarkResponse(BaseModel):
    benchmark_id: str
    name: str

@app.post("/benchmarks", status_code=201)
async def create_benchmark(body: CreateBenchmarkRequest) -> CreateBenchmarkResponse:
    """Create a new benchmark test case."""
    import uuid, datetime
    from datetime import timezone
    benchmark_id = str(uuid.uuid4())
    repo = get_repo()
    try:
        repo._ch.execute_many(
            """INSERT INTO otel.benchmarks
               (benchmark_id, suite, name, task_input, expected_output, rubric,
                difficulty, tags, dataset_version, created_at)
            VALUES""",
            [(
                benchmark_id, body.suite, body.name, body.task_input,
                body.expected_output, body.rubric, body.difficulty,
                body.tags, body.dataset_version,
                datetime.datetime.now(timezone.utc),
            )],
        )
        log.info("benchmark_created", benchmark_id=benchmark_id, name=body.name)
        return CreateBenchmarkResponse(benchmark_id=benchmark_id, name=body.name)
    except Exception as exc:
        log.error("create_benchmark_failed", error=str(exc))
        raise HTTPException(status_code=500, detail=str(exc))

@app.get("/benchmarks")
async def list_benchmarks(suite: str = "", limit: int = 100) -> list[dict]:
    """List benchmark test cases."""
    repo = get_repo()
    try:
        where = f"WHERE suite = %(suite)s" if suite else ""
        rows = repo._ch.fetch_all(
            f"SELECT benchmark_id, suite, name, task_input, expected_output, "
            f"rubric, difficulty, tags, dataset_version, created_at "
            f"FROM otel.benchmarks {where} ORDER BY created_at DESC LIMIT %(limit)s",
            {"suite": suite, "limit": limit},
        )
        return rows or []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


# ---- Execute benchmark run -----------------------------------------

class ExecuteRunRequest(BaseModel):
    agent_endpoint: str        # e.g. http://host.docker.internal:8090/chat
    benchmark_ids: list[str]   # list of benchmark_id UUIDs to run

@app.post("/runs/{run_id}/execute", status_code=202)
async def execute_run(
    run_id: str,
    body: ExecuteRunRequest,
    background_tasks: BackgroundTasks,
) -> dict:
    """
    Execute a benchmark run: send each benchmark task to the agent endpoint,
    collect trace IDs. Scores arrive automatically via OTel spans.
    """
    repo = get_repo()
    # Fetch the benchmark test cases
    try:
        placeholders = ", ".join(f"'{bid}'" for bid in body.benchmark_ids)
        benchmarks = repo._ch.fetch_all(
            f"SELECT benchmark_id, name, task_input FROM otel.benchmarks "
            f"WHERE benchmark_id IN ({placeholders})"
        ) if body.benchmark_ids else []
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Failed to fetch benchmarks: {exc}")

    if not benchmarks:
        raise HTTPException(status_code=404, detail="No benchmarks found for given IDs")

    background_tasks.add_task(
        _execute_benchmarks_background, run_id, benchmarks, body.agent_endpoint
    )
    return {
        "run_id": run_id,
        "queued_benchmarks": len(benchmarks),
        "agent_endpoint": body.agent_endpoint,
        "status": "executing",
    }

async def _execute_benchmarks_background(
    run_id: str, benchmarks: list[dict], agent_endpoint: str
) -> None:
    """Call agent endpoint for each benchmark task sequentially."""
    import httpx, asyncio
    log.info("benchmark_execution_started", run_id=run_id, count=len(benchmarks))
    results = []
    async with httpx.AsyncClient(timeout=120) as client:
        for bm in benchmarks:
            try:
                resp = await client.post(agent_endpoint, json={
                    "message": bm["task_input"],
                    "run_id": run_id,
                    "user_id": f"benchmark-{run_id[:8]}",
                    "source": "benchmark",
                })
                resp.raise_for_status()
                data = resp.json()
                results.append({
                    "benchmark_id": bm["benchmark_id"],
                    "trace_id": data.get("trace_id", ""),
                    "status": "ok",
                })
                log.info("benchmark_task_done", benchmark_id=bm["benchmark_id"], trace_id=data.get("trace_id"))
            except Exception as exc:
                log.error("benchmark_task_failed", benchmark_id=bm["benchmark_id"], error=str(exc))
                results.append({"benchmark_id": bm["benchmark_id"], "status": "error", "error": str(exc)})
            await asyncio.sleep(1)  # brief pause between tasks
    log.info("benchmark_execution_complete", run_id=run_id, results=results)
