from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, HttpUrl


class Platform(str, Enum):
    GREENHOUSE = "greenhouse"
    LEVER = "lever"
    ASHBY = "ashby"
    WORKDAY = "workday"
    LINKEDIN = "linkedin"
    INDEED = "indeed"
    ZIPRECRUITER = "ziprecruiter"


class Job(BaseModel):
    """A discovered job posting, normalized across platforms."""

    # Stable identifier for dedup. Format: "<platform>:<platform_id>".
    id: str
    platform: Platform
    platform_id: str  # the platform's native id for this posting

    company: str
    title: str
    location: str | None = None
    remote: bool = False
    employment_type: str | None = None  # "full_time", "contract", etc.

    description_text: str  # plain-text body used by the ranker
    description_html: str | None = None

    apply_url: str  # where to submit (may be an ATS URL, not the listing URL)
    listing_url: str  # human-readable posting page

    salary_min_usd: int | None = None
    salary_max_usd: int | None = None

    posted_at: datetime | None = None
    discovered_at: datetime = Field(default_factory=datetime.utcnow)

    # Raw payload for debugging / future re-parsing.
    raw: dict | None = None


class MatchScore(BaseModel):
    job_id: str
    score: int  # 0-100
    reasons: list[str] = Field(default_factory=list)
    dealbreakers: list[str] = Field(default_factory=list)


class ApplicationStatus(str, Enum):
    QUEUED = "queued"
    TAILORED = "tailored"
    SUBMITTED = "submitted"
    DRY_RUN = "dry_run"  # walked the form successfully but did not click submit
    FAILED = "failed"
    SKIPPED = "skipped"


class Application(BaseModel):
    job_id: str
    status: ApplicationStatus
    score: int | None = None
    resume_path: str | None = None
    cover_letter_path: str | None = None
    error: str | None = None
    submitted_at: datetime | None = None
    notes: str | None = None
