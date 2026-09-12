"""Telemetry — OTel setup and span emission for the gateway.

Emits 'gateway.llm_call' spans to the existing OTel Collector, for Jaeger
visualization only.

v4 architecture note (design/v2-gateway-capture-m1-ingest.md): this span is
NO LONGER an eval-trigger input. M1's GatewayIngestPipeline ingests directly
from `otel.gateway_call_log` (the durable call record written by db.log_call),
not from this span — that is what fixed the M1/M3 span-name mismatch
(gateway emitted 'gateway.llm_call', M1's trigger hardcoded 'agent.task').
Keep emitting it purely so Jaeger still shows gateway calls in its trace UI;
do not wire anything eval/governance-relevant to depend on this span again.
"""

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

log = logging.getLogger("gateway.telemetry")

_tracer: trace.Tracer | None = None


def init_telemetry() -> None:
    global _tracer
    endpoint = os.getenv(
        "OTEL_EXPORTER_OTLP_ENDPOINT",
        "http://otel-collector:4317",
    )
    resource = Resource.create({
        "service.name":    "agent-gateway",
        "service.version": "1.0.0",
        "eval.platform":   "aieval-local",
    })
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=endpoint, insecure=True)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("agent-gateway")
    log.info("OTel initialised → %s", endpoint)


def emit_call_span(record: dict) -> None:
    """Emit a gateway.llm_call span with the call record attributes."""
    if not _tracer:
        return
    try:
        with _tracer.start_as_current_span("gateway.llm_call") as span:
            span.set_attribute("gateway.call_id",          record.get("call_id",          ""))
            span.set_attribute("gateway.system_id",        record.get("system_id",        ""))
            span.set_attribute("gateway.agent_role",       record.get("agent_role",       ""))
            span.set_attribute("gateway.model_requested",  record.get("model_requested",  ""))
            span.set_attribute("gateway.model_used",       record.get("model_used",       ""))
            span.set_attribute("gateway.backend_used",     record.get("backend_used",     "openai"))
            span.set_attribute("gateway.routing_reason",   record.get("routing_reason",   ""))
            span.set_attribute("gateway.enforcement",      record.get("enforcement_result","pass"))
            span.set_attribute("gateway.is_shadow",        bool(record.get("is_shadow",   False)))
            span.set_attribute("gateway.latency_ms",       int(record.get("latency_ms",   0)))
            # Standard gen_ai attributes so eval-runner can pick up token counts
            span.set_attribute("gen_ai.request.model",        record.get("model_used",  ""))
            span.set_attribute("gen_ai.usage.input_tokens",   int(record.get("tokens_in",  0)))
            span.set_attribute("gen_ai.usage.output_tokens",  int(record.get("tokens_out", 0)))
            # Standard agent attributes for Jaeger hierarchy and eval grouping
            span.set_attribute("agent.role",      record.get("agent_role", ""))
            span.set_attribute("run.id",          record.get("run_id",     ""))
            span.set_attribute("trace.source",    "gateway")
            span.set_attribute("task.input",      record.get("prompt_text",   "")[:500])
            span.set_attribute("task.output",     record.get("response_text", "")[:500])
            mods = record.get("mods_applied", [])
            if mods:
                span.set_attribute("gateway.mods_applied", ",".join(str(m) for m in mods))
    except Exception as e:
        log.debug("emit_call_span error: %s", e)
