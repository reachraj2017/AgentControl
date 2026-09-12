"""Tests for acp_intelligence.IntelligenceClient."""
import pytest
from unittest.mock import MagicMock, patch

from acp_intelligence import IntelligenceClient


@pytest.fixture
def intel():
    return IntelligenceClient(evalgov_url="http://evalgov:8003")


class TestHealth:
    def test_returns_true_on_200(self, intel):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.get.return_value = mock_resp
            mock_cls.return_value = ctx
            assert intel.health() is True

    def test_returns_false_on_exception(self, intel):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("refused")
            assert intel.health() is False


class TestChat:
    def test_returns_response_string(self, intel):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "The trust score is 0.92"}
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_resp
            mock_cls.return_value = ctx
            result = intel.chat("What is the trust score?")
        assert result == "The trust score is 0.92"

    def test_returns_error_string_on_exception(self, intel):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("timeout")
            result = intel.chat("Hello")
        assert result.startswith("[error]")
        assert "timeout" in result

    def test_includes_history_in_payload(self, intel):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"response": "ok"}
        mock_resp.raise_for_status = MagicMock()
        history = [{"role": "user", "content": "hi"}, {"role": "assistant", "content": "hello"}]
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_resp
            mock_cls.return_value = ctx
            intel.chat("follow-up", history=history)
        payload = ctx.post.call_args[1]["json"]
        assert "history" in payload
        assert len(payload["history"]) == 2


class TestGetFindings:
    def test_returns_list(self, intel):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"findings": [{"severity": "high", "summary": "CB open"}]}
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.get.return_value = mock_resp
            mock_cls.return_value = ctx
            findings = intel.get_findings()
        assert len(findings) == 1
        assert findings[0]["severity"] == "high"

    def test_returns_empty_on_exception(self, intel):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("refused")
            assert intel.get_findings() == []


class TestRepr:
    def test_repr(self, intel):
        r = repr(intel)
        assert "evalgov:8003" in r
