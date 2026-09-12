"""Tests for acp_governance.GovernanceClient."""
import pytest
from unittest.mock import MagicMock, patch

from acp_governance import GovernanceClient


@pytest.fixture
def gov():
    return GovernanceClient(
        governance_url="http://governance:8002",
        agent_name="test-agent",
        system_id="test-system",
    )


class TestHealth:
    def test_returns_true_on_200(self, gov):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.get.return_value = mock_resp
            mock_cls.return_value = ctx
            assert gov.health() is True

    def test_returns_false_on_exception(self, gov):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("refused")
            assert gov.health() is False


class TestCheckPolicy:
    def test_returns_allow_on_network_error(self, gov):
        with patch("httpx.Client") as mock_cls:
            mock_cls.side_effect = Exception("timeout")
            result = gov.check_policy("send_email", {"recipient": "x@y.com"})
        assert result["decision"] == "allow"
        assert "error" in result

    def test_posts_correct_payload(self, gov):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"decision": "block", "reason": "pii"}
        mock_resp.raise_for_status = MagicMock()
        with patch("httpx.Client") as mock_cls:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_resp
            mock_cls.return_value = ctx
            result = gov.check_policy("send_email")
        assert result["decision"] == "block"
        call_kwargs = ctx.post.call_args
        payload = call_kwargs[1]["json"]
        assert payload["agent_name"] == "test-agent"
        assert payload["system_id"] == "test-system"
        assert payload["action"] == "send_email"


class TestCircuitBreaker:
    def test_is_open_true(self, gov):
        with patch.object(gov, "get_circuit_breaker", return_value={"state": "OPEN"}):
            assert gov.is_open() is True

    def test_is_open_false_when_closed(self, gov):
        with patch.object(gov, "get_circuit_breaker", return_value={"state": "closed"}):
            assert gov.is_open() is False

    def test_is_open_false_on_error(self, gov):
        with patch.object(gov, "get_circuit_breaker", return_value={"error": "not found"}):
            assert gov.is_open() is False


class TestTrustScore:
    def test_returns_float(self, gov):
        with patch.object(gov, "_get", return_value={"score": 0.85}):
            assert gov.get_trust_score() == 0.85

    def test_returns_1_on_missing(self, gov):
        with patch.object(gov, "_get", return_value={}):
            assert gov.get_trust_score() == 1.0


class TestWaitForApproval:
    def test_raises_timeout_error(self, gov):
        with patch.object(gov, "get_approval_status", return_value={"status": "pending"}):
            with pytest.raises(TimeoutError):
                gov.wait_for_approval("req-1", poll_interval=0.01, timeout=0.05)

    def test_returns_on_approval(self, gov):
        with patch.object(gov, "get_approval_status", return_value={"status": "approved"}):
            result = gov.wait_for_approval("req-1", poll_interval=0.01, timeout=5.0)
        assert result["status"] == "approved"


class TestRepr:
    def test_repr(self, gov):
        r = repr(gov)
        assert "governance:8002" in r
        assert "test-agent" in r
