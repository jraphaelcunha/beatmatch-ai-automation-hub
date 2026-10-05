"""
Data contracts and schema definitions for BeatMatch AI Automation Hub.
Enforces validation and serialization across scrapers, enrichers, and quality engines using Pydantic v2.
"""

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictBaseSchema(BaseModel):
    """Base schema enforcing strict type validation and prohibiting extra fields."""
    model_config = ConfigDict(strict=True, extra="forbid")

    def get(self, key: str, default: Any = None) -> Any:
        """Compatibility accessor supporting dictionary-style lookups."""
        return getattr(self, key, default)

    def __getitem__(self, item: str) -> Any:
        """Compatibility accessor supporting key indexing."""
        if hasattr(self, item):
            return getattr(self, item)
        raise KeyError(item)


class ArtistRecord(StrictBaseSchema):
    """Normalized artist entity extracted from Spotify and streaming platforms."""
    spotify_id: str = Field(..., min_length=1, description="Unique Spotify artist identifier")
    name: str = Field(..., min_length=1, description="Artist name")
    followers: int = Field(default=0, ge=0, description="Spotify follower count")
    popularity: int = Field(default=0, ge=0, le=100, description="Spotify popularity index (0-100)")
    status: Literal["NEW", "ENRICHED", "PENDING", "FAILED", "FAKED", "ACTIVE"] = Field(
        default="NEW",
        description="Pipeline processing state"
    )
    genres: list[str] = Field(default_factory=list, description="Associated music genres")
    instagram_url: str | None = Field(default=None, description="Discovered Instagram profile URL")


class LeadDiscoveryPayload(StrictBaseSchema):
    """Lead discovery contract dispatched to Monday.com Work OS and outreach queues."""
    lead_id: str = Field(..., min_length=1, description="Unique lead identifier")
    artist_name: str = Field(..., min_length=1, description="Artist or producer display name")
    platform: Literal["SPOTIFY", "YOUTUBE", "INSTAGRAM", "TWITTER"] = Field(
        default="SPOTIFY",
        description="Source platform"
    )
    profile_url: str = Field(..., description="Canonical link to the producer profile")
    confidence_score: float = Field(
        default=0.8,
        ge=0.0,
        le=1.0,
        description="Verification confidence level (0.0 to 1.0)"
    )
    discovered_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC discovery timestamp"
    )


class QualityMetrics(StrictBaseSchema):
    """Telemetry report recording SIPA data cleaning and deduplication metrics."""
    total_records: int = Field(default=0, ge=0, description="Total records evaluated in database")
    fakes_detected: int = Field(default=0, ge=0, description="Inactive or generic spam profiles flagged")
    deduplicated_count: int = Field(default=0, ge=0, description="Duplicate profiles pruned")
    active_leads: int = Field(default=0, ge=0, description="Production-grade leads retained")

    @property
    def clean_ratio(self) -> float:
        if self.total_records == 0:
            return 100.0
        return round((self.active_leads / self.total_records) * 100, 2)


class HostRunnerJob(StrictBaseSchema):
    """Job execution model for the background VPS host runner service."""
    job_id: str = Field(..., min_length=1, description="Unique task runner execution ID")
    task_name: str = Field(..., min_length=1, description="Pipeline job name")
    status: Literal["IDLE", "RUNNING", "COMPLETED", "FAILED"] = "IDLE"
    started_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc),
        description="UTC start time"
    )


class EnrichmentResult(StrictBaseSchema):
    """Output contract for artist social intelligence and streaming metric extraction."""
    instagram_url: str | None = Field(default=None, description="Extracted Instagram profile URL")
    monthly_listeners: int | None = Field(default=None, ge=0, description="Spotify monthly listeners")
    max_song_streams: int = Field(default=0, ge=0, description="Highest recorded stream count among top songs")
    is_verified: bool = Field(default=False, description="Verification badge status")


class TalentClassificationResult(StrictBaseSchema):
    """Contract for semantic evaluation of talent scouting leads performed by LLMs."""
    is_artist_promotion: bool = Field(description="Flag identifying emerging vocalist or recording artist")
    artist_name: str | None = Field(default=None, description="Discovered artist name if identifiable")
    reason: str = Field(default="", description="Classification explanation")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Model evaluation confidence")
