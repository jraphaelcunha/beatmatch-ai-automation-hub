"""
Unit tests for Instagram regex matching and handle normalization.
Verifies production regex imported directly from src.enrichers.instagram_finder.
"""

from src.enrichers.instagram_finder import INSTAGRAM_REGEX


def test_extracts_instagram_handle():
    """Valid canonical Instagram profile URLs must match and extract handle."""
    url = "https://www.instagram.com/metroboomin/"
    match = INSTAGRAM_REGEX.search(url)
    assert match is not None
    assert match.group(1).rstrip("/") == "metroboomin"


def test_extracts_handle_without_www():
    """HTTP links without www prefix and trailing query parameters must extract correctly."""
    url = "http://instagram.com/producer_tag123?utm_medium=copy"
    match = INSTAGRAM_REGEX.search(url)
    assert match is not None
    assert match.group(1) == "producer_tag123"


def test_ignores_invalid_urls():
    """Non-Instagram URLs must not match."""
    url = "https://twitter.com/metroboomin"
    assert INSTAGRAM_REGEX.search(url) is None
