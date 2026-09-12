"""Tests for acp_signals.client (checkpoint / handoff / tool_span) and context."""
import time
from unittest.mock import MagicMock, patch

import pytest

from acp_signals import context
from acp_signals.client import Decision, SignalsClient


@pytest.fixture
def client():
    return SignalsClient(gateway_url="http://gateway:8080", api_key="gw-sk-test")


@pytest.fixture(autouse=True)
def _reset_context():
    context.reset()
    yield
    context.reset()


class TestContext:
    def test_defaults_empty(self):
        ctx = context.get()
        assert ctx.conversation_id == ""
        assert ctx.agent_role == ""

    def test_partial_update_preserves_other_fields(self):
        context.set(conversation_id="conv-1", system_id="sys-1", agent_role="orchestrator")
        context.set(agent_role="summarizer")
        ctx = context.get()
        assert ctx.conversation_id == "conv-1"
        assert ctx.system_id == "sys-1"
        assert ctx.agent_role == "summarizer"


class TestCheckpoint:
    def test_posts_correct_payload_and_reads_context(self, client):
        context.set(conversation_id="conv-1", system_id="sys-1", agent_role="orchestrator")
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"decision": "block", "checkpoint_id": "cp-1", "reason": "pii"}
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_resp
            mock_cls.return_value = ctx
            decision = client.checkpoint("send_email", risk_level="high", metadata={"to": "x@y.com"})

        assert decision == Decision(decision="block", checkpoint_id="cp-1", reason="pii")
        payload = ctx.post.call_args[1]["json"]
        assert payload["action"] == "send_email"
        assert payload["risk_level"] == "high"
        assert payload["conversation_id"] == "conv-1"
        assert payload["system_id"] == "sys-1"
        assert payload["agent_role"] == "orchestrator"
        assert payload["schema_version"] == "1.0"

    def test_fails_open_on_network_error(self, client):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("connection refused")
            decision = client.checkpoint("delete_record", risk_level="critical")
        assert decision.decision == "allow"
        assert decision.error != ""
        assert decision.allowed is True


class TestHandoffAndToolSpan:
    def test_handoff_does_not_block_and_posts(self, client):
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            mock_cls.return_value = ctx
            client.handoff("orchestrator", "summarizer", context_summary="user wants a summary")
            # fire-and-forget runs on a background executor — give it a moment
            time.sleep(0.2)
        assert ctx.post.called
        payload = ctx.post.call_args[1]["json"]
        assert payload["from_agent"] == "orchestrator"
        assert payload["to_agent"] == "summarizer"

    def test_tool_span_swallows_errors(self, client):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("network down")
            # must not raise even though the POST will fail internally
            client.tool_span("web_search", input={"q": "x"}, output={"hits": 1}, latency_ms=100)
            time.sleep(0.2)


class TestRepr:
    def test_repr(self, client):
        assert "gateway:8080" in repr(client)
