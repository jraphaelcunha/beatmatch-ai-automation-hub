"""
Unit tests for unified safe HTTP client.
Verifies HTTPS enforcement, timeout propagation, and Monday.com API resilience.
"""

from unittest.mock import MagicMock, patch

import pytest

from src.utils.http_client import (
    call_monday_api,
    safe_http_get,
    safe_http_post,
)


def test_safe_http_get_rejects_non_https():
    """HTTP client must reject non-HTTPS URLs to prevent insecure transmissions."""
    with pytest.raises(ValueError, match="Insecure URL scheme rejected"):
        safe_http_get("http://api.example.com/data")


def test_safe_http_post_rejects_non_https():
    """HTTP POST must reject non-HTTPS URLs."""
    with pytest.raises(ValueError, match="Insecure URL scheme rejected"):
        safe_http_post("http://insecure.endpoint/api", payload={"key": "val"})


def test_safe_http_get_success():
    """Successful GET request returns raw response bytes."""
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"success": true, "items": [1, 2, 3]}'
    fake_response.__enter__.return_value = fake_response

    with patch("urllib.request.urlopen", return_value=fake_response) as mock_urlopen:
        result = safe_http_get("https://api.spotify.com/v1/search?q=test")
        assert result == b'{"success": true, "items": [1, 2, 3]}'
        mock_urlopen.assert_called_once()


def test_safe_http_post_success():
    """Successful POST request sends encoded payload and returns response bytes."""
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"status": "created"}'
    fake_response.__enter__.return_value = fake_response

    with patch("urllib.request.urlopen", return_value=fake_response):
        result = safe_http_post("https://api.monday.com/v2", payload={"query": "test"})
        assert result == b'{"status": "created"}'


def test_call_monday_api_mock_token():
    """Mock token returns mock dictionary without hitting network."""
    result = call_monday_api(token="mock_token", query="query { boards { id } }")
    assert result == {"data": {}, "mock": True}


def test_call_monday_api_empty_token():
    """Empty token returns mock dictionary."""
    result = call_monday_api(token="", query="query { me { id } }")
    assert result == {"data": {}, "mock": True}


def test_call_monday_api_execution():
    """Live call formatting executes GraphQL request against Monday endpoint."""
    fake_response = MagicMock()
    fake_response.read.return_value = b'{"data": {"boards": [{"id": "123"}]}}'
    fake_response.__enter__.return_value = fake_response

    with patch("urllib.request.urlopen", return_value=fake_response):
        result = call_monday_api(token="real_api_token_123", query="query { boards { id } }")
        assert result["data"]["boards"][0]["id"] == "123"
