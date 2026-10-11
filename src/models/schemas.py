"""
Data contracts and schema definitions for BeatMatch AI Automation Hub.
Enforces validation and serialization across scrapers, enrichers, and quality engines using Pydantic v2.
"""

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class LeadStatus(StrEnum):
    """Canonical lifecycle states for the BeatMatch lead pipeline."""
    PENDING_SPOTIFY = "pending_spotify"
    PENDING_INSTAGRAM = "pending_instagram"
    READY_FOR_SIPA = "ready_for_sipa"
    PROCESSING = "processing"
    PROCESSED = "processed"
    FAILED_SPOTIFY = "failed_spotify"
    FAILED_INSTAGRAM = "failed_instagram"
    SKIPPED_NO_INSTA = "skipped_no_insta"
    SKIPPED_NO_SPOTIFY = "skipped_no_spotify"
    SKIPPED_TOO_FAMOUS = "skipped_too_famous"
    GARBAGE = "garbage"


VALID_TRANSITIONS: dict[LeadStatus, set[LeadStatus]] = {
    LeadStatus.PENDING_SPOTIFY: {
        LeadStatus.PENDING_SPOTIFY,
        LeadStatus.PENDING_INSTAGRAM,
        LeadStatus.READY_FOR_SIPA,
        LeadStatus.SKIPPED_NO_SPOTIFY,
        LeadStatus.FAILED_SPOTIFY,
        LeadStatus.GARBAGE,
    },
    LeadStatus.PENDING_INSTAGRAM: {
        LeadStatus.PENDING_INSTAGRAM,
        LeadStatus.READY_FOR_SIPA,
        LeadStatus.SKIPPED_NO_INSTA,
        LeadStatus.SKIPPED_TOO_FAMOUS,
        LeadStatus.FAILED_INSTAGRAM,
        LeadStatus.GARBAGE,
    },
    LeadStatus.READY_FOR_SIPA: {
        LeadStatus.READY_FOR_SIPA,
        LeadStatus.PROCESSING,
        LeadStatus.PROCESSED,
        LeadStatus.GARBAGE,
    },
    LeadStatus.PROCESSING: {
        LeadStatus.PROCESSING,
        LeadStatus.PROCESSED,
        LeadStatus.READY_FOR_SIPA,
        LeadStatus.GARBAGE,
    },
    LeadStatus.PROCESSED: {
        LeadStatus.PROCESSED,
        LeadStatus.READY_FOR_SIPA,  # re-export
    },
    LeadStatus.SKIPPED_NO_SPOTIFY: {
        LeadStatus.SKIPPED_NO_SPOTIFY,
        LeadStatus.PENDING_SPOTIFY,  # retry upon alias update
    },
    LeadStatus.SKIPPED_NO_INSTA: {
        LeadStatus.SKIPPED_NO_INSTA,
        LeadStatus.PENDING_INSTAGRAM,  # retry upon social re-crawl
    },
    LeadStatus.SKIPPED_TOO_FAMOUS: {
        LeadStatus.SKIPPED_TOO_FAMOUS,  # terminal
    },
    LeadStatus.FAILED_SPOTIFY: {
        LeadStatus.FAILED_SPOTIFY,
        LeadStatus.PENDING_SPOTIFY,  # retry
    },
    LeadStatus.FAILED_INSTAGRAM: {
        LeadStatus.FAILED_INSTAGRAM,
        LeadStatus.PENDING_INSTAGRAM,  # retry
    },
    LeadStatus.GARBAGE: {
        LeadStatus.GARBAGE,  # terminal
    },
}


def validate_transition(current: LeadStatus | str, target: LeadStatus | str) -> bool:
    """
    Validates whether a transition from current to target status is permitted.
    Raises ValueError on unrecognized status values; returns boolean for validity.
    """
    try:
        current_status = LeadStatus(current)
    except ValueError as err:
        raise ValueError(f"Invalid current lead status: '{current}'") from err

    try:
        target_status = LeadStatus(target)
    except ValueError as err:
        raise ValueError(f"Invalid target lead status: '{target}'") from err

    allowed = VALID_TRANSITIONS.get(current_status, set())
    return target_status in allowed


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
    status: LeadStatus = Field(
        default=LeadStatus.PENDING_SPOTIFY,
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
        default_factory=lambda: datetime.now(UTC),
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
        default_factory=lambda: datetime.now(UTC),
        description="UTC start time"
    )


class EnrichmentResult(StrictBaseSchema):
    """Output contract for artist social intelligence and streaming metric extraction."""
    instagram_url: str | None = Field(default=None, description="Extracted Instagram profile URL")
    monthly_listeners: int | None = Field(default=None, ge=0, description="Spotify monthly listeners")
    max_song_streams: int = Field(default=0, ge=0, description="Highest recorded stream count among top songs")
    is_verified: bool = Field(default=False, description="Verification badge status")


class GeminiTelemetry(StrictBaseSchema):
    """Inference execution telemetry and provider usage metrics."""
    latency_ms: float | None = Field(default=None, ge=0.0, description="Latency of the HTTP attempt in milliseconds")
    total_duration_ms: float | None = Field(default=None, ge=0.0, description="Total duration across all attempts in milliseconds")
    input_tokens: int | None = Field(default=None, ge=0, description="Prompt tokens reported by provider usageMetadata")
    output_tokens: int | None = Field(default=None, ge=0, description="Candidate tokens reported by provider usageMetadata")
    total_tokens: int | None = Field(default=None, ge=0, description="Total tokens reported by provider usageMetadata")
    model_version: str | None = Field(default=None, description="Model version string reported by provider")
    requested_model: str = Field(default="gemini-2.5-flash", description="Model requested in API endpoint")
    attempts: int = Field(default=1, ge=0, description="Number of HTTP attempts executed")


class TalentClassificationResult(StrictBaseSchema):
    """Contract for semantic evaluation of talent scouting leads performed by LLMs."""
    is_artist_promotion: bool = Field(description="Flag identifying emerging vocalist or recording artist")
    artist_name: str | None = Field(default=None, description="Discovered artist name if identifiable")
    reason: str = Field(default="", description="Classification explanation")
    confidence: float = Field(default=0.8, ge=0.0, le=1.0, description="Model evaluation confidence")
    status: Literal["SUCCESS", "FALLBACK", "ERROR"] = Field(
        default="SUCCESS",
        description="Execution status differentiating genuine predictions from operational errors and fallbacks"
    )
    error_message: str | None = Field(default=None, description="Operational error details if status is ERROR")
    telemetry: GeminiTelemetry | None = Field(default=None, description="Inference execution telemetry")
