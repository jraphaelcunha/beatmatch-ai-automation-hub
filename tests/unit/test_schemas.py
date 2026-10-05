"""
Unit tests for data contracts in BeatMatch AI Automation Hub.
Verifies strict validation, boundary constraints, and new enrichment models under Pydantic v2.
"""

from pydantic import ValidationError
import pytest

from src.models.schemas import (
    ArtistRecord,
    EnrichmentResult,
    LeadDiscoveryPayload,
    QualityMetrics,
    TalentClassificationResult,
)


def test_artist_record_valid(sample_artist_record):
    assert sample_artist_record.name == "Kaytranada"
    assert sample_artist_record.followers == 2450000
    assert sample_artist_record.popularity == 72
    assert sample_artist_record.status == "ACTIVE"


def test_artist_record_popularity_bounds():
    with pytest.raises(ValidationError):
        ArtistRecord(
            spotify_id="test_id",
            name="Test Artist",
            popularity=150
        )


def test_artist_record_forbids_extra_fields():
    with pytest.raises(ValidationError):
        ArtistRecord(
            spotify_id="test_id",
            name="Test Artist",
            unmapped_extra_field="malicious_payload"
        )


def test_lead_discovery_payload(sample_lead_payload):
    assert isinstance(sample_lead_payload, LeadDiscoveryPayload)
    assert sample_lead_payload.lead_id == "LEAD-2026-001"
    assert sample_lead_payload.platform == "SPOTIFY"
    assert 0.0 <= sample_lead_payload.confidence_score <= 1.0


def test_quality_metrics_clean_ratio():
    metrics = QualityMetrics(
        total_records=54000,
        fakes_detected=4000,
        deduplicated_count=5000,
        active_leads=45000
    )
    assert metrics.clean_ratio == 83.33

    empty_metrics = QualityMetrics()
    assert empty_metrics.clean_ratio == 100.0


def test_enrichment_result_contract():
    result = EnrichmentResult(
        instagram_url="https://www.instagram.com/producer_flow",
        monthly_listeners=5200,
        max_song_streams=8900
    )
    assert result.instagram_url == "https://www.instagram.com/producer_flow"
    assert result.monthly_listeners == 5200
    assert result["max_song_streams"] == 8900
    assert result.get("non_existent", "fallback") == "fallback"


def test_talent_classification_result_contract():
    classification = TalentClassificationResult(
        is_artist_promotion=True,
        artist_name="MC Underground",
        reason="Promoting upcoming EP release",
        confidence=0.92
    )
    assert classification.is_artist_promotion is True
    assert classification.artist_name == "MC Underground"
    assert classification.get("confidence") == 0.92
