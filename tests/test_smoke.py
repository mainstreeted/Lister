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


def test_coarse_filter_keeps_remote_and_filters_location():
    """Coarse filter no longer rejects on title — that's the ranker's job."""
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
    remote_eng = Job(
        id="t:1",
        platform=Platform.GREENHOUSE,
        platform_id="1",
        company="X",
        title="Account Executive",  # off-target title — should STILL pass
        location="Remote",
        remote=True,
        description_text="",
        apply_url="u",
        listing_url="u",
    )
    onsite = remote_eng.model_copy(
        update={"id": "t:2", "location": "London, UK", "remote": False}
    )
    excluded = remote_eng.model_copy(update={"id": "t:3", "company": "Banned"})
    crit_excl = crit.model_copy(
        update={"filters": crit.filters.model_copy(update={"exclude_companies": ["Banned"]})}
    )
    assert c._passes_coarse_filter(remote_eng, crit) is True
    assert c._passes_coarse_filter(onsite, crit) is False
    assert c._passes_coarse_filter(excluded, crit_excl) is False


def test_transient_http_error_classification():
    from lister.discovery.greenhouse import _is_transient_http_error
    import httpx as _httpx

    # 404 is NOT transient.
    resp_404 = _httpx.Response(404, request=_httpx.Request("GET", "https://x/"))
    err_404 = _httpx.HTTPStatusError("nope", request=resp_404.request, response=resp_404)
    assert _is_transient_http_error(err_404) is False

    # 503 IS transient.
    resp_503 = _httpx.Response(503, request=_httpx.Request("GET", "https://x/"))
    err_503 = _httpx.HTTPStatusError("retry", request=resp_503.request, response=resp_503)
    assert _is_transient_http_error(err_503) is True

    # Timeouts are transient.
    assert _is_transient_http_error(_httpx.TimeoutException("slow")) is True


def test_batch_response_parsing_round_trips_job_ids():
    """ClaudeCliRanker._parse_batch_response should pair scores back to jobs by id."""
    from lister.ranker import _parse_batch_response

    j1 = Job(
        id="greenhouse:foo:1",
        platform=Platform.GREENHOUSE,
        platform_id="1",
        company="Foo",
        title="Ops Manager",
        location="Remote",
        remote=True,
        description_text="",
        apply_url="u",
        listing_url="u",
    )
    j2 = j1.model_copy(update={"id": "greenhouse:foo:2", "title": "AE"})

    # Model output with prose around the JSON array — common in CLI mode.
    text = """Here are the scores for both jobs:

[
  {"job_id": "greenhouse:foo:1", "score": 82, "reasons": ["title match", "remote"], "dealbreakers": []},
  {"job_id": "greenhouse:foo:2", "score": 15, "reasons": ["sales role"], "dealbreakers": ["wrong function"]}
]

Hope that helps."""
    scores = _parse_batch_response(text, [j1, j2])
    assert len(scores) == 2
    assert scores[0].job_id == "greenhouse:foo:1"
    assert scores[0].score == 82
    assert scores[1].score == 15
    assert "wrong function" in scores[1].dealbreakers


def test_linkedin_jsonld_extraction():
    """LinkedIn embeds JobPosting JSON-LD; we should pull it out of any wrapper."""
    from lister.discovery.linkedin import _find_job_posting, _parse_jsonld, _parse_location

    # Bare object
    bare = {"@type": "JobPosting", "title": "Test"}
    assert _find_job_posting(bare) == bare

    # Wrapped in @graph
    wrapped = {"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "X"}]}
    assert _find_job_posting(wrapped) == {"@type": "JobPosting", "title": "X"}

    # Real-ish LinkedIn page snippet
    html = """
    <html><head>
    <script type="application/ld+json">
    {"@context":"https://schema.org","@type":"JobPosting","title":"Customer Success Manager",
     "description":"<p>Lead enterprise accounts.</p>",
     "hiringOrganization":{"@type":"Organization","name":"Acme"},
     "jobLocation":{"@type":"Place","address":{"addressLocality":"Remote","addressCountry":"US"}},
     "datePosted":"2026-05-01"}
    </script>
    </head><body></body></html>
    """
    data = _parse_jsonld(html)
    assert data is not None
    assert data["title"] == "Customer Success Manager"
    assert data["hiringOrganization"]["name"] == "Acme"

    # Location parsing handles dict, list-of-dict, and string.
    assert _parse_location("Remote") == "Remote"
    assert _parse_location({"address": {"addressLocality": "Austin", "addressRegion": "TX"}}) == "Austin, TX"
    assert _parse_location(
        [{"address": {"addressLocality": "A"}}, {"address": {"addressLocality": "B"}}]
    ) == "A; B"
    assert _parse_location(None) is None


def test_batch_response_handles_missing_job():
    """If the model drops a job from its response, we synthesize a placeholder."""
    from lister.ranker import _parse_batch_response

    j1 = Job(
        id="a",
        platform=Platform.GREENHOUSE,
        platform_id="1",
        company="A",
        title="X",
        location="Remote",
        remote=True,
        description_text="",
        apply_url="u",
        listing_url="u",
    )
    j2 = j1.model_copy(update={"id": "b"})
    # Only job "a" appears in output.
    text = '[{"job_id": "a", "score": 50, "reasons": [], "dealbreakers": []}]'
    scores = _parse_batch_response(text, [j1, j2])
    assert len(scores) == 2
    assert scores[0].score == 50
    assert scores[1].score == 0
    assert "missing" in scores[1].reasons[0].lower()
