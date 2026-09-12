"""acp-tracing — OTLP span instrumentation for the AI Control Plane (M1)."""

from acp_tracing.tracer import ACPTracer, SpanContext, AgentSpan, instrument, get_tracer
from acp_tracing.otel_ecosystem import configure_otlp_exporter

__all__ = [
    "ACPTracer",
    "SpanContext",
    "AgentSpan",
    "instrument",
    "get_tracer",
    "configure_otlp_exporter",
]
__version__ = "0.1.0"
