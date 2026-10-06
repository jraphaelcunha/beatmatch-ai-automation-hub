"""
Unit tests for deterministic PII sanitization and Gemini classifier fallback contracts.
"""

import json
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
