"""Tests for acp_tracing.ACPTracer."""
import pytest
from unittest.mock import MagicMock, patch

from acp_tracing import ACPTracer, SpanContext


@pytest.fixture
def tracer():
    return ACPTracer(
        otlp_endpoint="http://localhost:8000",
        agent_name="test-agent",
        agent_role="tester",
        system_id="test-system",
    )


class TestSpanContext:
    def test_defaults(self):
        ctx = SpanContext()
        assert ctx.span_id != ""
        assert ctx.trace_id != ""
        assert ctx.prompt == ""
        assert ctx.tokens_in == 0

    def test_unique_ids(self):
        ctx1 = SpanContext()
        ctx2 = SpanContext()
        assert ctx1.span_id != ctx2.span_id
        assert ctx1.trace_id != ctx2.trace_id


class TestSpanContextManager:
    def test_yields_context(self, tracer):
        with patch.object(tracer, "_export"):
            with tracer.span(model="gpt-4o") as ctx:
                assert isinstance(ctx, SpanContext)
                assert ctx.model == "gpt-4o"

    def test_calls_export_on_exit(self, tracer):
        with patch.object(tracer, "_export") as mock_export:
            with tracer.span(model="gpt-4o") as ctx:
                ctx.prompt = "hello"
            mock_export.assert_called_once()
            exported_ctx = mock_export.call_args[0][0]
            assert exported_ctx.prompt == "hello"

    def test_calls_export_on_exception(self, tracer):
        with patch.object(tracer, "_export") as mock_export:
            with pytest.raises(ValueError):
                with tracer.span(model="gpt-4o") as ctx:
                    raise ValueError("test error")
            mock_export.assert_called_once()
            exported_ctx = mock_export.call_args[0][0]
            assert "test error" in exported_ctx.error

    def test_latency_set_after_exit(self, tracer):
        with patch.object(tracer, "_export") as mock_export:
            with tracer.span(model="gpt-4o"):
                pass
            ctx = mock_export.call_args[0][0]
            assert ctx.latency_ms >= 0

    def test_conversation_id_in_attributes(self, tracer):
        with patch.object(tracer, "_export") as mock_export:
            with tracer.span(model="gpt-4o", conversation_id="conv-xyz"):
                pass
            ctx = mock_export.call_args[0][0]
            assert ctx.attributes.get("acp.conversation_id") == "conv-xyz"


class TestRecordCall:
    def test_returns_span_context(self, tracer):
        with patch.object(tracer, "_export"):
            ctx = tracer.record_call(
                model="gpt-4o",
                prompt="hello",
                completion="world",
                tokens_in=10,
                tokens_out=5,
                latency_ms=200.0,
            )
        assert isinstance(ctx, SpanContext)
        assert ctx.model == "gpt-4o"
        assert ctx.tokens_in == 10

    def test_export_called(self, tracer):
        with patch.object(tracer, "_export") as mock_export:
            tracer.record_call(model="gpt-4o", prompt="x", completion="y")
        mock_export.assert_called_once()


class TestExportHTTP:
    def test_silent_on_network_error(self, tracer):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("connection refused")
            ctx = SpanContext(model="gpt-4o")
            tracer._export_http(ctx)  # should not raise
