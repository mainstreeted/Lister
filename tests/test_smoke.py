"""Tests that don't touch the network or the LLM."""
from __future__ import annotations

from datetime import datetime

import pytest

from lister.discovery.greenhouse import GreenhouseConnector, _html_to_text, _parse_dt
from lister.models import Job, Platform


def test_html_to_text_strips_tags():
    html = "<p>Hello <b>world</b></p><script>x=1</script>"
    text = _html_to_text(html)
    assert "Hello" in text
    assert "world" in text
    assert "x=1" not in text


def test_parse_dt_iso():
    assert _parse_dt("2025-01-15T12:00:00Z") == datetime(2025, 1, 15, 12, 0, 0)
    assert _parse_dt(None) is None
    assert _parse_dt("garbage") is None


def test_normalize_job_minimal():
    c = GreenhouseConnector()
    raw = {
        "id": 12345,
        "title": "Senior Backend Engineer",
        "location": {"name": "Remote - US"},
        "absolute_url": "https://boards.greenhouse.io/example/jobs/12345",
        "content": "<p>We build cool stuff in Python.</p>",
        "updated_at": "2025-01-01T00:00:00Z",
    }
    job = c._normalize("example", raw)
    assert isinstance(job, Job)
    assert job.platform == Platform.GREENHOUSE
    assert job.platform_id == "12345"
    assert job.id == "greenhouse:example:12345"
    assert job.remote is True
    assert "Python" in job.description_text


def test_coarse_filter_excludes_off_target_titles(tmp_path, monkeypatch):
    from lister.config import (
        Criteria,
        CoverLetterConfig,
        DailyConfig,
        Filters,
        PlatformConfig,
        PlatformsConfig,
        Profile,
        ResumesConfig,
        Targets,
    )

    crit = Criteria(
        profile=Profile(name="t", email="t@t.com"),
        targets=Targets(titles=["Engineer"]),
        filters=Filters(locations=["Remote"], remote_ok=True),
        platforms=PlatformsConfig(greenhouse=PlatformConfig(enabled=True)),
        daily=DailyConfig(),
        resumes=ResumesConfig(granular="x", formal="y"),
        cover_letter=CoverLetterConfig(),
    )
    c = GreenhouseConnector()
    eng = Job(
        id="t:1",
        platform=Platform.GREENHOUSE,
        platform_id="1",
        company="X",
        title="Backend Engineer",
        location="Remote",
        remote=True,
        description_text="",
        apply_url="u",
        listing_url="u",
    )
    sales = eng.model_copy(update={"id": "t:2", "title": "Account Executive"})
    assert c._passes_coarse_filter(eng, crit) is True
    assert c._passes_coarse_filter(sales, crit) is False
