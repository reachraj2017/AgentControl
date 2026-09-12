"""Tests for acp_sdk.ACPClient."""
import pytest
from unittest.mock import MagicMock, patch

from acp_sdk.client import ACPClient


@pytest.fixture
def acp():
    return ACPClient(
        gateway_url="http://gw:8080",
        eval_runner_url="http://eval:8000",
        governance_url="http://gov:8002",
        evalgov_url="http://intel:8003",
        api_key="gw-sk-test",
        agent_name="tester",
        agent_role="test-role",
        system_id="test-sys",
    )


class TestRepr:
    def test_repr(self, acp):
        r = repr(acp)
        assert "tester" in r
        assert "test-role" in r
        assert "test-sys" in r


class TestLazyLoading:
    def test_gateway_raises_import_error_when_missing(self, acp):
        with patch.dict("sys.modules", {"acp_gateway": None}):
            with pytest.raises((ImportError, TypeError)):
                _ = acp.gateway

    def test_governance_raises_import_error_when_missing(self, acp):
        with patch.dict("sys.modules", {"acp_governance": None}):
            with pytest.raises((ImportError, TypeError)):
                _ = acp.governance

    def test_intelligence_raises_import_error_when_missing(self, acp):
        with patch.dict("sys.modules", {"acp_intelligence": None}):
            with pytest.raises((ImportError, TypeError)):
                _ = acp.intelligence


class TestGatewayProperty:
    def test_returns_gateway_client(self, acp):
        mock_gw = MagicMock()
        mock_cls = MagicMock(return_value=mock_gw)
        with patch("acp_sdk.client.ACPClient.gateway.fget", None):
            with patch("acp_gateway.GatewayClient", mock_cls):
                gw = acp.gateway
        # Verify it's cached — second access returns same instance
        gw2 = acp.gateway
        assert gw is gw2


class TestHealthMethod:
    def test_health_returns_dict(self, acp):
        mock_gw = MagicMock()
        mock_gw.health.return_value = True
        mock_gov = MagicMock()
        mock_gov.health.return_value = True
        mock_intel = MagicMock()
        mock_intel.health.return_value = False

        acp._gateway = mock_gw
        acp._governance = mock_gov
        acp._intelligence = mock_intel

        result = acp.health()
        assert result.get("gateway") is True
        assert result.get("governance") is True
        assert result.get("evalgov") is False

    def test_health_skips_missing_modules(self, acp):
        acp._gateway = None
        acp._governance = None
        acp._intelligence = None

        with patch("acp_sdk.client.ACPClient.gateway.fget", property(lambda self: (_ for _ in ()).throw(ImportError("no gateway")))):
            result = acp.health()
        # Should not raise — returns empty dict or partial
        assert isinstance(result, dict)
