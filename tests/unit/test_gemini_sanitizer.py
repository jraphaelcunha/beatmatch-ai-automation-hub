"""
Unit tests for deterministic PII sanitization and Gemini classifier fallback contracts.
"""

import json
import urllib.error
from unittest.mock import MagicMock, patch

from src.utils.gemini_classifier import classify_lead_with_gemini, sanitize_pii


def test_sanitize_pii_removes_emails():
    """Confirms email addresses are redacted prior to LLM submission."""
    raw_text = "Check out my music, reach me at artist_booking@domain.com or prod@gmail.com"
    sanitized = sanitize_pii(raw_text)
    assert "artist_booking@domain.com" not in sanitized
    assert "prod@gmail.com" not in sanitized
    assert "[EMAIL_REDACTED]" in sanitized


def test_sanitize_pii_removes_phone_numbers():
    """Confirms phone numbers are redacted prior to LLM submission."""
    raw_text = "Call me for feat: +55 21 98765-4321 or (11) 91234-5678"
    sanitized = sanitize_pii(raw_text)
    assert "98765-4321" not in sanitized
    assert "91234-5678" not in sanitized
    assert "[PHONE_REDACTED]" in sanitized


def test_sanitize_pii_handles_clean_text():
    """Text without PII must remain untouched."""
    clean_input = "Check out my new single on Spotify! Song title: Midnight Echoes"
    assert sanitize_pii(clean_input) == clean_input


def test_gemini_unconfigured_fallback():
    """Without valid API key, must return deterministic fallback safely without crashing."""
    result = classify_lead_with_gemini("Check my track: Midnight Echoes")
    assert result.is_artist_promotion is False
    assert result.confidence == 0.0
    assert result.get("is_artist_promotion") is False


def test_gemini_classified_artist_promotion():
    """Valid Gemini API response for artist promotion must be parsed into contract."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "is_artist_promotion": True,
                        "artist_name": "Luna Ray",
                        "reason": "Comment contains explicit request to listen to their music",
                        "confidence": 0.95
                    })
                }]
            }
        }]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = classify_lead_with_gemini("Check my new single on Spotify! I am Luna Ray")
        assert result.is_artist_promotion is True
        assert result.artist_name == "Luna Ray"
        assert result.confidence == 0.95


def test_gemini_classified_casual_listener():
    """Comment from casual listener must be classified as negative promotion."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "is_artist_promotion": False,
                        "artist_name": None,
                        "reason": "Comment is appreciation of the producer beat",
                        "confidence": 0.9
                    })
                }]
            }
        }]
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = classify_lead_with_gemini("This beat is crazy hard!")
        assert result.is_artist_promotion is False
        assert result.artist_name is None


def test_gemini_success_with_usage_metadata_and_model_version():
    """Verify successful inference captures real usageMetadata, modelVersion, and latency."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "is_artist_promotion": True,
                        "artist_name": "Luna Ray",
                        "reason": "Explicit promotion",
                        "confidence": 0.95
                    })
                }]
            }
        }],
        "usageMetadata": {
            "promptTokenCount": 185,
            "candidatesTokenCount": 32,
            "totalTokenCount": 217
        },
        "modelVersion": "gemini-2.5-flash-001"
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = classify_lead_with_gemini("Check my new single on Spotify! I am Luna Ray")
        assert result.is_artist_promotion is True
        assert result.artist_name == "Luna Ray"
        assert result.telemetry is not None
        assert result.telemetry.input_tokens == 185
        assert result.telemetry.output_tokens == 32
        assert result.telemetry.total_tokens == 217
        assert result.telemetry.model_version == "gemini-2.5-flash-001"
        assert result.telemetry.requested_model == "gemini-2.5-flash"
        assert result.telemetry.latency_ms is not None
        assert result.telemetry.latency_ms >= 0.0
        assert result.telemetry.attempts == 1


def test_gemini_retry_429_then_success():
    """Verify HTTP 429 triggers retry with mock sleep and succeeds on attempt 2."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "is_artist_promotion": True,
                        "artist_name": "Luna Ray",
                        "reason": "Explicit promotion",
                        "confidence": 0.95
                    })
                }]
            }
        }],
        "usageMetadata": {
            "promptTokenCount": 185,
            "candidatesTokenCount": 32,
            "totalTokenCount": 217
        },
        "modelVersion": "gemini-2.5-flash-001"
    }

    mock_resp_success = MagicMock()
    mock_resp_success.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp_success.__enter__.return_value = mock_resp_success

    http_429_err = urllib.error.HTTPError(
        url="https://generativelanguage.googleapis.com",
        code=429,
        msg="Too Many Requests",
        hdrs=None,
        fp=None
    )

    mock_sleep = MagicMock()

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", side_effect=[http_429_err, mock_resp_success]):
        result = classify_lead_with_gemini("Check my track", max_retries=3, sleep_fn=mock_sleep)
        assert result.is_artist_promotion is True
        assert result.artist_name == "Luna Ray"
        assert result.telemetry is not None
        assert result.telemetry.attempts == 2
        assert result.telemetry.input_tokens == 185
        assert result.telemetry.output_tokens == 32
        assert result.telemetry.total_tokens == 217
        assert mock_sleep.call_count == 1


def test_gemini_timeout_fallback():
    """Simulates network timeout/URLError and confirms deterministic fallback contract."""
    timeout_err = urllib.error.URLError("timed out")

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", side_effect=timeout_err):
        result = classify_lead_with_gemini("Check my track")
        assert result.is_artist_promotion is False
        assert result.confidence == 0.0
        assert result.reason == "Evaluation exhausted retries or failed network call"
        assert result.telemetry is not None
        assert result.telemetry.input_tokens is None
        assert result.telemetry.output_tokens is None
        assert result.telemetry.total_tokens is None
        assert result.telemetry.model_version is None
        assert result.telemetry.attempts == 1
        assert result.telemetry.latency_ms is not None


def test_gemini_response_missing_usage_metadata():
    """Confirms valid response without usageMetadata sets token counts to None without heuristic fallback."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "is_artist_promotion": True,
                        "artist_name": "Luna Ray",
                        "reason": "Promotion detected",
                        "confidence": 0.9
                    })
                }]
            }
        }],
        "modelVersion": "gemini-2.5-flash-001"
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = classify_lead_with_gemini("Check my track")
        assert result.is_artist_promotion is True
        assert result.telemetry is not None
        assert result.telemetry.input_tokens is None
        assert result.telemetry.output_tokens is None
        assert result.telemetry.total_tokens is None
        assert result.telemetry.model_version == "gemini-2.5-flash-001"


def test_gemini_response_missing_model_version():
    """Confirms valid response without modelVersion keeps model_version None without fabricating values."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{
                    "text": json.dumps({
                        "is_artist_promotion": False,
                        "artist_name": None,
                        "reason": "No promotion",
                        "confidence": 0.85
                    })
                }]
            }
        }],
        "usageMetadata": {
            "promptTokenCount": 110,
            "candidatesTokenCount": 20,
            "totalTokenCount": 130
        }
    }

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp):
        result = classify_lead_with_gemini("Just a casual comment")
        assert result.is_artist_promotion is False
        assert result.telemetry is not None
        assert result.telemetry.input_tokens == 110
        assert result.telemetry.output_tokens == 20
        assert result.telemetry.total_tokens == 130
        assert result.telemetry.model_version is None
        assert result.telemetry.requested_model == "gemini-2.5-flash"


def test_gemini_request_uses_header_auth_and_clean_url():
    """Confirms request passes API key via x-goog-api-key header and keeps URL clean of query parameters."""
    mock_payload = {
        "candidates": [{
            "content": {
                "parts": [{"text": json.dumps({"is_artist_promotion": False, "confidence": 0.8})}]
            }
        }]
    }
    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps(mock_payload).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeSecretKey999"}), \
         patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        result = classify_lead_with_gemini("Check my sound")
        assert result.status == "SUCCESS"

        # Inspect Request object passed to urlopen
        req = mock_urlopen.call_args[0][0]
        assert "key=" not in req.full_url
        assert "AIzaSy" not in req.full_url
        assert req.full_url == "https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent"
        assert req.headers.get("X-goog-api-key") == "AIzaSyFakeSecretKey999"


def test_gemini_network_error_does_not_log_api_key(caplog):
    """Verifies network failures do not expose the private API key in application logs."""
    import logging
    timeout_err = urllib.error.URLError("Connection refused")

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSySecretDoNotLeak"}), \
         patch("urllib.request.urlopen", side_effect=timeout_err), \
         caplog.at_level(logging.WARNING):
        result = classify_lead_with_gemini("Check my track")
        assert result.status == "ERROR"
        assert "AIzaSySecretDoNotLeak" not in caplog.text


def test_gemini_error_on_empty_candidates_or_malformed_json():
    """Confirms empty candidate list or corrupted JSON yields status='ERROR'."""
    # Subcase A: Empty candidates
    mock_resp_empty = MagicMock()
    mock_resp_empty.read.return_value = json.dumps({"candidates": []}).encode("utf-8")
    mock_resp_empty.__enter__.return_value = mock_resp_empty

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp_empty):
        res_empty = classify_lead_with_gemini("Test comment")
        assert res_empty.status == "ERROR"
        assert "empty candidate set" in res_empty.reason.lower()

    # Subcase B: Malformed JSON in part text
    mock_resp_bad_json = MagicMock()
    mock_resp_bad_json.read.return_value = json.dumps({
        "candidates": [{"content": {"parts": [{"text": "Not valid JSON {"}]}}]
    }).encode("utf-8")
    mock_resp_bad_json.__enter__.return_value = mock_resp_bad_json

    with patch.dict("os.environ", {"GEMINI_API_KEY": "AIzaSyFakeKeyValidFormat12345"}), \
         patch("urllib.request.urlopen", return_value=mock_resp_bad_json):
        res_bad = classify_lead_with_gemini("Test comment")
        assert res_bad.status == "ERROR"
        assert "json" in res_bad.reason.lower()
