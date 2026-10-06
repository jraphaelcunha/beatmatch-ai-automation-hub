"""
Pytest global fixtures and configurations for BeatMatch AI Automation Hub test suite.
"""

import pytest

from src.models.schemas import ArtistRecord, LeadDiscoveryPayload


@pytest.fixture
def sample_artist_record():
    """Valid ArtistRecord instance."""
    return ArtistRecord(
        spotify_id="4zCH9qm4RITtNQecJrIT84",
        name="Kaytranada",
        followers=2450000,
        popularity=72,
        status="ACTIVE",
        genres=["electronic", "hip hop", "r&b"],
        instagram_url="https://www.instagram.com/kaytranada"
    )


@pytest.fixture
def sample_lead_payload():
    """Valid LeadDiscoveryPayload instance."""
    return LeadDiscoveryPayload(
        lead_id="LEAD-2026-001",
        artist_name="Metro Boomin",
        platform="SPOTIFY",
        profile_url="https://open.spotify.com/artist/0iEtIxbK0KxaSlF7G42ZOp",
        confidence_score=0.95
    )
