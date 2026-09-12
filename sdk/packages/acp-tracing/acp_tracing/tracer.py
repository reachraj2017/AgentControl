"""
acp_tracing.tracer
~~~~~~~~~~~~~~~~~~
Wraps the OpenTelemetry SDK to emit LLM-call spans to the AI Control Plane
eval-runner (M1) via OTLP/HTTP.

Spans follow the ACP semantic convention used by the eval-runner:
  - span name: the agent_name you pass
  - gen_ai.* attributes for model, tokens, prompt, completion
  - acp.* attributes for agent role, system id, conversation id

The eval-runner will pick up these spans, run 68 eval metrics, and store
results in ClickHouse for the portal and EvalGov agent to query.
"""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Generator


@dataclass
class SpanContext:
    """Holds mutable state for a live LLM span."""

    span_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    trace_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    start_time: float = field(default_factory=time.time)

    prompt: str = ""
    completion: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    latency_ms: float = 0.0
    error: str = ""
    attributes: dict[str, Any] = field(default_factory=dict)


class ACPTracer:
    """
    Instruments LLM calls and exports OTLP spans to the ACP eval-runner.

    Args:
        otlp_endpoint: OTLP/HTTP endpoint of the ACP eval-runner
                       (e.g. ``http://localhost:8000/v1/traces``).
        agent_name:    Name of the calling agent — appears as the span name
                       and is used for per-agent dashboards.
        agent_role:    Role tag (matches the gateway ``X-Gateway-Agent-Role`` header).
        system_id:     Calling system identifier.
        service_name:  OpenTelemetry ``service.name`` resource attribute.

    Example — context manager::

        from acp_tracing import ACPTracer

        tracer = ACPTracer(
            otlp_endpoint="http://localhost:8000/v1/traces",
            agent_name="summarizer",
            agent_role="summarizer",
            system_id="my-product",
        )

        with tracer.span(model="gpt-4o-mini") as ctx:
            ctx.prompt = user_message
            response = call_llm(user_message)          # your existing call
            ctx.completion = response.choices[0].message.content
            ctx.tokens_in = response.usage.prompt_tokens
            ctx.tokens_out = response.usage.completion_tokens
        # span is exported when the `with` block exits

    Example — decorator::

        @tracer.trace(model="gpt-4o-mini")
        def summarize(text: str) -> str:
            ...
            return summary
    """

    def __init__(
        self,
        otlp_endpoint: str = "http://localhost:8000/v1/traces",
        agent_name: str = "agent",
        agent_role: str = "default",
        system_id: str = "*",
        service_name: str = "acp-instrumented-agent",
    ) -> None:
        self.otlp_endpoint = otlp_endpoint.rstrip("/")
        self.agent_name = agent_name
        self.agent_role = agent_role
        self.system_id = system_id
        self.service_name = service_name
        self._otel_available = self._check_otel()

    # ── Public API ─────────────────────────────────────────────────────────────

    @contextmanager
    def span(
        self,
        model: str = "",
        conversation_id: str = "",
        extra: dict[str, Any] | None = None,
    ) -> Generator[SpanContext, None, None]:
        """
        Context manager that wraps a single LLM call.

        Yields a :class:`SpanContext` you populate with prompt/completion/tokens.
        Exports the span on exit (success or exception).
        """
        ctx = SpanContext(model=model)
        ctx.attributes = extra or {}
        if conversation_id:
            ctx.attributes["acp.conversation_id"] = conversation_id
        try:
            yield ctx
        except Exception as exc:
            ctx.error = str(exc)
            raise
        finally:
            ctx.latency_ms = (time.time() - ctx.start_time) * 1000
            self._export(ctx)

    def trace(self, model: str = "", conversation_id: str = "", **span_kwargs: Any):
        """Decorator that wraps a function returning a string completion."""

        def decorator(fn):
            def wrapper(*args, **kwargs):
                with self.span(model=model, conversation_id=conversation_id, extra=span_kwargs) as ctx:
                    result = fn(*args, **kwargs)
                    if isinstance(result, str):
                        ctx.completion = result
                    return result

            wrapper.__name__ = fn.__name__
            return wrapper

        return decorator

    def record_call(
        self,
        *,
        model: str,
        prompt: str,
        completion: str,
        tokens_in: int = 0,
        tokens_out: int = 0,
        latency_ms: float = 0.0,
        conversation_id: str = "",
        error: str = "",
        extra: dict[str, Any] | None = None,
    ) -> SpanContext:
        """
        One-shot: record a completed LLM call and export immediately.

        Useful when you already have all the data (e.g. from a raw API response).
        Returns the :class:`SpanContext` so callers can read ``span_id`` / ``trace_id``.
        """
        ctx = SpanContext(model=model)
        ctx.prompt = prompt
        ctx.completion = completion
        ctx.tokens_in = tokens_in
        ctx.tokens_out = tokens_out
        ctx.latency_ms = latency_ms
        ctx.error = error
        ctx.attributes = extra or {}
        if conversation_id:
            ctx.attributes["acp.conversation_id"] = conversation_id
        self._export(ctx)
        return ctx

    # ── Export ─────────────────────────────────────────────────────────────────

    def _export(self, ctx: SpanContext) -> None:
        if self._otel_available:
            self._export_otel(ctx)
        else:
            self._export_http(ctx)

    def _export_otel(self, ctx: SpanContext) -> None:
        """Export via the opentelemetry-sdk if installed."""
        try:
            from opentelemetry import trace  # noqa: PLC0415
            from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
            from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415
            from opentelemetry.sdk.resources import Resource  # noqa: PLC0415

            resource = Resource(attributes={"service.name": self.service_name})
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(endpoint=f"{self.otlp_endpoint}/v1/traces")
            provider.add_span_processor(BatchSpanProcessor(exporter))

            otel_tracer = provider.get_tracer(self.service_name)
            with otel_tracer.start_as_current_span(self.agent_name) as span:
                span.set_attribute("gen_ai.system", "openai")
                span.set_attribute("gen_ai.request.model", ctx.model)
                span.set_attribute("gen_ai.prompt", ctx.prompt[:8192])
                span.set_attribute("gen_ai.completion", ctx.completion[:8192])
                span.set_attribute("gen_ai.usage.prompt_tokens", ctx.tokens_in)
                span.set_attribute("gen_ai.usage.completion_tokens", ctx.tokens_out)
                span.set_attribute("acp.agent_role", self.agent_role)
                span.set_attribute("acp.system_id", self.system_id)
                span.set_attribute("acp.latency_ms", ctx.latency_ms)
                if ctx.error:
                    span.set_attribute("error", ctx.error)
                for k, v in ctx.attributes.items():
                    span.set_attribute(k, v)
        except Exception:
            self._export_http(ctx)

    def _export_http(self, ctx: SpanContext) -> None:
        """Fallback: POST a minimal OTLP-JSON span directly."""
        try:
            import httpx  # noqa: PLC0415

            now_ns = int(ctx.start_time * 1e9)
            end_ns = now_ns + int(ctx.latency_ms * 1e6)
            payload = {
                "resourceSpans": [
                    {
                        "resource": {
                            "attributes": [
                                {"key": "service.name", "value": {"stringValue": self.service_name}}
                            ]
                        },
                        "scopeSpans": [
                            {
                                "spans": [
                                    {
                                        "traceId": ctx.trace_id.replace("-", ""),
                                        "spanId": ctx.span_id.replace("-", "")[:16],
                                        "name": self.agent_name,
                                        "startTimeUnixNano": str(now_ns),
                                        "endTimeUnixNano": str(end_ns),
                                        "attributes": [
                                            {"key": "gen_ai.request.model", "value": {"stringValue": ctx.model}},
                                            {"key": "gen_ai.prompt", "value": {"stringValue": ctx.prompt[:8192]}},
                                            {"key": "gen_ai.completion", "value": {"stringValue": ctx.completion[:8192]}},
                                            {"key": "gen_ai.usage.prompt_tokens", "value": {"intValue": str(ctx.tokens_in)}},
                                            {"key": "gen_ai.usage.completion_tokens", "value": {"intValue": str(ctx.tokens_out)}},
                                            {"key": "acp.agent_role", "value": {"stringValue": self.agent_role}},
                                            {"key": "acp.system_id", "value": {"stringValue": self.system_id}},
                                            {"key": "acp.latency_ms", "value": {"doubleValue": ctx.latency_ms}},
                                        ],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
            with httpx.Client(timeout=5.0) as c:
                c.post(
                    f"{self.otlp_endpoint}/v1/traces",
                    json=payload,
                    headers={"Content-Type": "application/json"},
                )
        except Exception:
            pass  # tracing must never crash the host application

    @staticmethod
    def _check_otel() -> bool:
        try:
            import opentelemetry  # noqa: F401,PLC0415
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: F401,PLC0415
            return True
        except ImportError:
            return False


# ── Framework-style global TracerProvider API ──────────────────────────────────
# For ADK, LangChain, and other frameworks that auto-instrument via OTel and
# expect a global TracerProvider to be set up once at process startup.

def instrument(
    service_name: str,
    agent_role: str = "default",
    otlp_endpoint: str = "http://localhost:8000",
    system_id: str = "*",
) -> None:
    """
    Set up a global OTel TracerProvider that exports to the ACP eval-runner.

    Call once at process startup. Frameworks like ADK and LangChain will then
    automatically attach their spans to this provider.

    Args:
        service_name:  Logical name for your agent service (``service.name`` resource attr).
        agent_role:    Role tag sent on every span as ``acp.agent_role``.
        otlp_endpoint: ACP eval-runner base URL (default ``http://localhost:8000``).
        system_id:     System/deployment identifier.
    """
    try:
        from opentelemetry import trace  # noqa: PLC0415
        from opentelemetry.sdk.resources import Resource  # noqa: PLC0415
        from opentelemetry.sdk.trace import TracerProvider  # noqa: PLC0415
        from opentelemetry.sdk.trace.export import BatchSpanProcessor  # noqa: PLC0415
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter  # noqa: PLC0415

        resource = Resource.create({
            "service.name": service_name,
            "acp.agent_role": agent_role,
            "acp.system_id": system_id,
        })
        endpoint = otlp_endpoint.rstrip("/")
        if not endpoint.endswith("/v1/traces"):
            endpoint = f"{endpoint}/v1/traces"
        exporter = OTLPSpanExporter(endpoint=endpoint)
        provider = TracerProvider(resource=resource)
        provider.add_span_processor(BatchSpanProcessor(exporter))
        trace.set_tracer_provider(provider)
    except ImportError:
        pass  # opentelemetry-sdk not installed — silently skip


def get_tracer(name: str = "acp_tracing") -> Any:
    """
    Return a named tracer from the current global TracerProvider.

    Requires :func:`instrument` to have been called first, or for another
    part of the application to have set a global TracerProvider.
    """
    try:
        from opentelemetry import trace  # noqa: PLC0415
        return trace.get_tracer(name)
    except ImportError:
        return None


class AgentSpan:
    """
    Context manager that wraps a single agent operation in an OTel span.

    For use with ADK and other frameworks where OTel is already configured
    globally via :func:`instrument`. Automatically sets ACP span attributes
    and records exceptions.

    Example::

        with AgentSpan("process_query", agent_role="researcher") as span:
            result = call_llm(prompt)
            span.set_attribute("gen_ai.completion", result)
            span.set_llm_tokens(tokens_in=512, tokens_out=128)
    """

    def __init__(
        self,
        span_name: str,
        agent_role: str = "",
        task_input: str = "",
        llm_model: str = "",
        tracer_name: str = "acp_tracing",
    ) -> None:
        self._span_name = span_name
        self._agent_role = agent_role
        self._task_input = task_input
        self._llm_model = llm_model
        self._tracer_name = tracer_name
        self._cm = None
        self._span = None

    def __enter__(self) -> "AgentSpan":
        try:
            from opentelemetry import trace  # noqa: PLC0415
            t = trace.get_tracer(self._tracer_name)
            self._cm = t.start_as_current_span(self._span_name)
            self._span = self._cm.__enter__()
            if self._agent_role:
                self._span.set_attribute("acp.agent_role", self._agent_role)
            if self._task_input:
                self._span.set_attribute("gen_ai.prompt", self._task_input)
            if self._llm_model:
                self._span.set_attribute("gen_ai.request.model", self._llm_model)
        except ImportError:
            pass
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self._cm is not None:
            return self._cm.__exit__(exc_type, exc_val, exc_tb)
        return False

    def set_attribute(self, key: str, value: Any) -> "AgentSpan":
        if self._span is not None:
            self._span.set_attribute(key, str(value) if not isinstance(value, (bool, int, float, str)) else value)
        return self

    def set_llm_tokens(self, tokens_in: int = 0, tokens_out: int = 0) -> "AgentSpan":
        if tokens_in:
            self.set_attribute("gen_ai.usage.prompt_tokens", tokens_in)
        if tokens_out:
            self.set_attribute("gen_ai.usage.completion_tokens", tokens_out)
        return self
