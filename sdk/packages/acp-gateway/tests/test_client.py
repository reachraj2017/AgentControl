"""Tests for acp_gateway.GatewayClient."""
import pytest
from unittest.mock import MagicMock, patch

from acp_gateway import GatewayClient


@pytest.fixture
def gw():
    return GatewayClient(
        gateway_url="http://gateway:8080",
        api_key="gw-sk-test",
        agent_role="tester",
        system_id="test-system",
        conversation_id="conv-123",
    )


class TestHeaders:
    def test_all_headers_present(self, gw):
        h = gw._headers()
        assert h["X-Gateway-Agent-Role"] == "tester"
        assert h["X-Gateway-System-Id"] == "test-system"
        assert h["Authorization"] == "Bearer gw-sk-test"
        assert h["X-Gateway-Conversation-Id"] == "conv-123"

    def test_no_auth_header_when_no_key(self):
        gw = GatewayClient(gateway_url="http://gw:8080")
        h = gw._headers()
        assert "Authorization" not in h

    def test_no_conversation_header_when_no_id(self):
        gw = GatewayClient(gateway_url="http://gw:8080")
        h = gw._headers()
        assert "X-Gateway-Conversation-Id" not in h

    def test_role_headers_no_content_type(self, gw):
        h = gw._role_headers()
        assert "Content-Type" not in h
        assert h["X-Gateway-Agent-Role"] == "tester"


class TestHealth:
    def test_returns_true_on_200(self, gw):
        mock_response = MagicMock()
        mock_response.status_code = 200
        with patch("httpx.Client") as mock_client_class:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.get.return_value = mock_response
            mock_client_class.return_value = ctx
            assert gw.health() is True

    def test_returns_false_on_exception(self, gw):
        with patch("httpx.Client") as mock_client_class:
            mock_client_class.side_effect = Exception("connection refused")
            assert gw.health() is False


class TestPost:
    def test_returns_json_on_success(self, gw):
        mock_response = MagicMock()
        mock_response.json.return_value = {"choices": [{"message": {"content": "hi"}}]}
        mock_response.raise_for_status = MagicMock()
        with patch("httpx.Client") as mock_client_class:
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=ctx)
            ctx.__exit__ = MagicMock(return_value=False)
            ctx.post.return_value = mock_response
            mock_client_class.return_value = ctx
            result = gw.chat_openai("gpt-4o-mini", [{"role": "user", "content": "hi"}])
        assert result["choices"][0]["message"]["content"] == "hi"

    def test_returns_error_dict_on_exception(self, gw):
        with patch("httpx.Client") as mock_client_class:
            mock_client_class.side_effect = Exception("timeout")
            result = gw._post("/v1/chat/completions", {})
        assert "error" in result
        assert "timeout" in result["error"]


class TestOpenAIClient:
    def test_raises_import_error_without_openai(self, gw):
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "openai":
                raise ImportError("no openai")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="openai package"):
                gw.openai_client()

    def test_raises_import_error_without_anthropic(self, gw):
        import builtins
        real_import = builtins.__import__

        def mock_import(name, *args, **kwargs):
            if name == "anthropic":
                raise ImportError("no anthropic")
            return real_import(name, *args, **kwargs)

        with patch("builtins.__import__", side_effect=mock_import):
            with pytest.raises(ImportError, match="anthropic package"):
                gw.anthropic_client()


class TestRepr:
    def test_repr(self, gw):
        r = repr(gw)
        assert "gateway:8080" in r
        assert "tester" in r
        assert "test-system" in r
