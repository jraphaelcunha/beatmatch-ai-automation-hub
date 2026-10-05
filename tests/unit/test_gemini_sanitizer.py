"""
Unit tests for deterministic PII sanitization and Gemini classifier fallback contracts.
"""

from src.utils.gemini_classifier import classify_lead_with_gemini, sanitize_pii


def test_sanitize_pii_removes_emails():
    raw_text = "Check out my music, reach me at artist_booking@domain.com or prod@gmail.com"
    sanitized = sanitize_pii(raw_text)
    assert "artist_booking@domain.com" not in sanitized
    assert "prod@gmail.com" not in sanitized
    assert "[EMAIL_REDACTED]" in sanitized


def test_sanitize_pii_removes_phone_numbers():
    raw_text = "Call me for feat: +55 21 98765-4321 or (11) 91234-5678"
    sanitized = sanitize_pii(raw_text)
    assert "98765-4321" not in sanitized
    assert "91234-5678" not in sanitized
    assert "[PHONE_REDACTED]" in sanitized


def test_sanitize_pii_handles_clean_text():
    clean_input = "Check out my new single on Spotify! Song title: Midnight Echoes"
    assert sanitize_pii(clean_input) == clean_input


def test_gemini_unconfigured_fallback():
    # Without valid API key, must return deterministic fallback safely without crashing
    result = classify_lead_with_gemini("Check my track: Midnight Echoes")
    assert result.is_artist_promotion is False
    assert result.confidence == 0.0
    assert result.get("is_artist_promotion") is False
