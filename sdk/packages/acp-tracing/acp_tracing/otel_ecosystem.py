"""
acp_tracing.otel_ecosystem
~~~~~~~~~~~~~~~~~~~~~~~~~~
Alternative to :func:`acp_tracing.instrument` — configures a plain OTLP span
exporter pointed at the ACP collector, for use ALONGSIDE a community-maintained
OpenInference or OpenLLMetry auto-instrumentor, instead of ACP providing its
own passive per-framework tracer.

Why this exists
----------------
``acp_tracing.instrument()`` sets up a global ``TracerProvider`` and expects
the calling framework to attach its own spans to it. That's a reasonable
default for a framework ACP already knows well (e.g. Google ADK in
``opt-demo``), but it does not generalize safely to arbitrary external agent
frameworks — passive, ACP-owned instrumentation is exactly what broke against
real external agent systems in production (see
docs/external-agent-integration-findings.md, Issues 1, 2, 5: wrong redirect
method, one role header for all agents, and a competing tracing system).

OpenInference (github.com/Arize-ai/openinference) and OpenLLMetry
(github.com/traceloop/openllmetry) already maintain auto-instrumentors for a
wide swath of frameworks and provider SDKs (OpenAI, Anthropic, Google/Vertex,
LangChain, LlamaIndex, CrewAI, Bedrock, and more), emitting OTel GenAI
semantic-convention-aligned spans. Rather than re-deriving that per-framework
correctness ourselves, this module gives you the one thing ACP actually needs
to own — where the spans go — and gets out of the way of how they're
produced.

Usage::

    # 1. Install a community instrumentor for your framework/provider, e.g.:
    #      pip install openinference-instrumentation-openai
    #    or:
    #      pip install traceloop-sdk

    from acp_tracing.otel_ecosystem import configure_otlp_exporter
    configure_otlp_exporter(endpoint="http://localhost:4318")

    # 2a. OpenInference:
    from openinference.instrumentation.openai import OpenAIInstrumentor
    OpenAIInstrumentor().instrument()

    # 2b. OR OpenLLMetry (Traceloop) — this one manages its own exporter setup,
    #     so pass the endpoint directly instead of calling configure_otlp_exporter:
    from traceloop.sdk import Traceloop
    Traceloop.init(app_name="my-agent", api_endpoint="http://localhost:4318", disable_batch=True)

M1's ingestion pipeline reads OTel GenAI semantic-convention attributes
(``gen_ai.*``) natively — see design/checkpoint-handoff-ingest.md §7 and
design/v2-gateway-capture-m1-ingest.md §5.2's ``task_span_names`` /
attribute-alias work — so spans from either ecosystem are picked up without
requiring ACP's own span shape.
"""

from __future__ import annotations

from typing import Optional


def configure_otlp_exporter(
    endpoint: str = "http://localhost:4318",
    service_name: str = "acp-instrumented-agent",
    headers: Optional[dict[str, str]] = None,
) -> bool:
    """
    Register a global OTel ``TracerProvider`` exporting to ``endpoint`` via
    OTLP/HTTP, with no ACP-specific span logic — just plumbing. Call this
    once at process startup, BEFORE initializing your chosen OpenInference
    instrumentor (OpenLLMetry's ``Traceloop.init()`` manages its own exporter
    and does not need this call — pass its ``api_endpoint`` argument instead).

    Returns ``True`` if the OTel SDK was available and the provider was
    registered, ``False`` if ``opentelemetry-sdk`` is not installed (in which
    case this is a silent no-op, matching the rest of this package's
    "tracing must never crash the host application" posture).
    """
    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        resource = Resource.create({"service.name": service_name})
        trace_endpoint = endpoint.rstrip("/")
        if not trace_endpoint.endswith("/v1/traces"):
            trace_endpoint = f"{trace_endpoint}/v1/traces"
        exporter = OTLPSpanExporter(endpoint=trace_endpoint, headers=headers or {})
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
        return True
    except ImportError:
        return False
