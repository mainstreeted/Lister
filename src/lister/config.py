from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CRITERIA_PATH = REPO_ROOT / "config" / "criteria.yaml"


class ProfileLinks(BaseModel):
    linkedin: str = ""
    github: str = ""
    portfolio: str = ""


class Profile(BaseModel):
    name: str
    email: str
    phone: str = ""
    location: str = ""
    pronouns: str = ""
    links: ProfileLinks = Field(default_factory=ProfileLinks)


class Targets(BaseModel):
    titles: list[str] = Field(default_factory=list)
    preferred_skills: list[str] = Field(default_factory=list)
    exclude_required_skills: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)


class Filters(BaseModel):
    locations: list[str] = Field(default_factory=list)
    remote_ok: bool = True
    min_salary_usd: int = 0
    max_age_days: int = 30
    exclude_companies: list[str] = Field(default_factory=list)
    needs_sponsorship: bool = False
    has_security_clearance: bool = False
    # Free-text constraints passed verbatim to the LLM ranker. Use this for
    # rules too nuanced to encode as flags (e.g. "Atlanta hybrid only if 100k+").
    special_constraints: str = ""


class PlatformConfig(BaseModel):
    enabled: bool = False
    companies: list[str] = Field(default_factory=list)
    search_queries: list[str] = Field(default_factory=list)


class PlatformsConfig(BaseModel):
    greenhouse: PlatformConfig = Field(default_factory=PlatformConfig)
    lever: PlatformConfig = Field(default_factory=PlatformConfig)
    ashby: PlatformConfig = Field(default_factory=PlatformConfig)
    workday: PlatformConfig = Field(default_factory=PlatformConfig)
    linkedin: PlatformConfig = Field(default_factory=PlatformConfig)
    indeed: PlatformConfig = Field(default_factory=PlatformConfig)
    ziprecruiter: PlatformConfig = Field(default_factory=PlatformConfig)


class DailyConfig(BaseModel):
    max_submissions_per_day: int = 5
    max_per_platform: int = 1
    min_match_score: int = 70
    dry_run: bool = True


class ResumesConfig(BaseModel):
    granular: str
    formal: str


class CoverLetterConfig(BaseModel):
    tone: str = "warm, direct, specific"
    max_words: int = 250
    must_include: list[str] = Field(default_factory=list)
    banned_phrases: list[str] = Field(default_factory=list)


class Criteria(BaseModel):
    profile: Profile
    targets: Targets
    filters: Filters
    platforms: PlatformsConfig
    daily: DailyConfig
    resumes: ResumesConfig
    cover_letter: CoverLetterConfig = Field(default_factory=CoverLetterConfig)


def load_criteria(path: Path | None = None) -> Criteria:
    """Load criteria.yaml, falling back to the example if the user file isn't present."""
    target = path or DEFAULT_CRITERIA_PATH
    if not target.exists():
        example = REPO_ROOT / "config" / "criteria.example.yaml"
        raise FileNotFoundError(
            f"No criteria file at {target}. Copy {example} to {target} and edit."
        )
    data: dict[str, Any] = yaml.safe_load(target.read_text())
    return Criteria.model_validate(data)


def load_resume(rel_path: str) -> str:
    """Load a resume markdown file by repo-relative path."""
    p = REPO_ROOT / rel_path
    if not p.exists():
        raise FileNotFoundError(
            f"Resume not found at {p}. See config/resumes/README.md for format."
        )
    return p.read_text()
