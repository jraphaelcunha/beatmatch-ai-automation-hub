"""
Unit tests for data contracts in BeatMatch AI Automation Hub.
Verifies strict validation, boundary constraints, and new enrichment models under Pydantic v2.
"""

import pytest
from pydantic import ValidationError

from src.models.schemas import (
    ArtistRecord,
    EnrichmentResult,
    GeminiTelemetry,
    LeadDiscoveryPayload,
    LeadStatus,
    QualityMetrics,
    TalentClassificationResult,
    validate_transition,
)


def test_artist_record_valid(sample_artist_record):
    assert sample_artist_record.name == "Kaytranada"
    assert sample_artist_record.followers == 2450000
    assert sample_artist_record.popularity == 72
    assert sample_artist_record.status == LeadStatus.READY_FOR_SIPA
    assert sample_artist_record.status.value == "ready_for_sipa"


def test_artist_record_default_status():
    record = ArtistRecord(spotify_id="test_id_default", name="Default Artist")
    assert record.status == LeadStatus.PENDING_SPOTIFY
    assert record.status.value == "pending_spotify"


def test_artist_record_accepts_all_canonical_states():
    for state in LeadStatus:
        record = ArtistRecord(spotify_id="test_state_id", name="Artist", status=state)
        assert record.status == state


def test_artist_record_rejects_invalid_states():
    invalid_statuses = ["ACTIVE", "NEW", "pending_enrichment", "FAILED", "unknown_state"]
    for inv in invalid_statuses:
        with pytest.raises(ValidationError):
            ArtistRecord(spotify_id="test_inv_id", name="Artist", status=inv)


def test_validate_transition_canonical_paths():
    # Valid forward progressions
    assert validate_transition(LeadStatus.PENDING_SPOTIFY, LeadStatus.PENDING_INSTAGRAM) is True
    assert validate_transition(LeadStatus.PENDING_SPOTIFY, LeadStatus.READY_FOR_SIPA) is True
    assert validate_transition(LeadStatus.PENDING_SPOTIFY, LeadStatus.SKIPPED_NO_SPOTIFY) is True
    assert validate_transition(LeadStatus.PENDING_SPOTIFY, LeadStatus.FAILED_SPOTIFY) is True
    assert validate_transition(LeadStatus.PENDING_INSTAGRAM, LeadStatus.READY_FOR_SIPA) is True
    assert validate_transition(LeadStatus.PENDING_INSTAGRAM, LeadStatus.SKIPPED_NO_INSTA) is True
    assert validate_transition(LeadStatus.PENDING_INSTAGRAM, LeadStatus.SKIPPED_TOO_FAMOUS) is True
    assert validate_transition(LeadStatus.READY_FOR_SIPA, LeadStatus.PROCESSING) is True
    assert validate_transition(LeadStatus.READY_FOR_SIPA, LeadStatus.PROCESSED) is True
    assert validate_transition(LeadStatus.READY_FOR_SIPA, LeadStatus.GARBAGE) is True
    assert validate_transition(LeadStatus.PROCESSING, LeadStatus.PROCESSED) is True

    # Valid string arguments
    assert validate_transition("pending_spotify", "pending_instagram") is True


def test_validate_transition_idempotent_self_transitions():
    for state in LeadStatus:
        assert validate_transition(state, state) is True
        assert validate_transition(state.value, state.value) is True


def test_validate_transition_retry_semantics():
    assert validate_transition(LeadStatus.FAILED_SPOTIFY, LeadStatus.PENDING_SPOTIFY) is True
    assert validate_transition(LeadStatus.FAILED_INSTAGRAM, LeadStatus.PENDING_INSTAGRAM) is True
    assert validate_transition(LeadStatus.SKIPPED_NO_SPOTIFY, LeadStatus.PENDING_SPOTIFY) is True
    assert validate_transition(LeadStatus.SKIPPED_NO_INSTA, LeadStatus.PENDING_INSTAGRAM) is True


def test_validate_transition_rejects_invalid_transitions():
    assert validate_transition(LeadStatus.PENDING_SPOTIFY, LeadStatus.PROCESSED) is False
    assert validate_transition(LeadStatus.GARBAGE, LeadStatus.READY_FOR_SIPA) is False
    assert validate_transition(LeadStatus.SKIPPED_TOO_FAMOUS, LeadStatus.READY_FOR_SIPA) is False
    assert validate_transition(LeadStatus.PROCESSED, LeadStatus.PENDING_SPOTIFY) is False


def test_validate_transition_unknown_status_raises():
    with pytest.raises(ValueError, match="Invalid current lead status"):
        validate_transition("non_existent", "pending_spotify")

    with pytest.raises(ValueError, match="Invalid target lead status"):
        validate_transition("pending_spotify", "non_existent")


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


def test_gemini_telemetry_contract():
    telemetry = GeminiTelemetry(
        latency_ms=124.5,
        total_duration_ms=130.2,
        input_tokens=180,
        output_tokens=30,
        total_tokens=210,
        model_version="gemini-2.5-flash-001",
        requested_model="gemini-2.5-flash",
        attempts=1
    )
    classification = TalentClassificationResult(
        is_artist_promotion=True,
        artist_name="MC Underground",
        reason="Promoting upcoming EP",
        confidence=0.9,
        telemetry=telemetry
    )
    assert classification.telemetry is not None
    assert classification.telemetry.latency_ms == 124.5
    assert classification.telemetry.input_tokens == 180
    assert classification.telemetry.output_tokens == 30
    assert classification.telemetry.total_tokens == 210
    assert classification.telemetry.model_version == "gemini-2.5-flash-001"
    assert classification.telemetry.attempts == 1
