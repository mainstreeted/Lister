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


def test_email_digest_indeed_parser_extracts_jobs():
    """Indeed digest emails embed jk= job-keys in tracking links."""
    import email.message
    from lister.discovery.email_digest import parse_indeed

    html = """
    <html><body>
      <table>
        <tr>
          <td>
            <a href="https://www.indeed.com/rc/clk?jk=abc123def&from=alert">
              Senior Customer Service Manager
            </a>
            <br/>Acme Insurance · Tallahassee, FL
          </td>
        </tr>
        <tr>
          <td>
            <a href="https://www.indeed.com/viewjob?jk=zzz999">
              Underwriter, Personal Lines
            </a>
            <br/>BigCo · Remote
          </td>
        </tr>
        <tr><td><a href="https://www.indeed.com/manage/alerts">unsubscribe</a></td></tr>
      </table>
    </body></html>
    """
    msg = email.message.EmailMessage()
    msg["Subject"] = "5 new jobs for you"
    msg["From"] = "alert@indeed.com"
    msg.set_content(html, subtype="html")

    jobs = list(parse_indeed(msg))
    assert len(jobs) == 2
    assert jobs[0].platform_id == "abc123def"
    assert jobs[0].id == "indeed:email:abc123def"
    assert "Customer Service Manager" in jobs[0].title
    assert jobs[1].platform_id == "zzz999"
    assert jobs[1].remote is True


def test_email_digest_linkedin_parser_extracts_jobs():
    import email.message
    from lister.discovery.email_digest import parse_linkedin

    html = """
    <html><body>
      <a href="https://www.linkedin.com/comm/jobs/view/4123456789/?some=tracking">
        Customer Success Manager
      </a>
      <span>Stripe · Remote · Posted 2 days ago</span>
      <a href="https://www.linkedin.com/jobs/view/9876543210/">
        Insurance Underwriter
      </a>
      <span>State Farm · Tallahassee, FL</span>
    </body></html>
    """
    msg = email.message.EmailMessage()
    msg["Subject"] = "Jobs for you"
    msg["From"] = "jobs-noreply@linkedin.com"
    msg.set_content(html, subtype="html")

    jobs = list(parse_linkedin(msg))
    assert len(jobs) == 2
    assert jobs[0].platform_id == "4123456789"
    assert jobs[1].platform_id == "9876543210"


def test_email_digest_url_extractors():
    from lister.discovery.email_digest import (
        _extract_indeed_jk,
        _extract_li_job_id,
        _extract_zr_id,
    )

    assert _extract_indeed_jk("https://www.indeed.com/rc/clk?jk=abc12345") == "abc12345"
    assert _extract_indeed_jk("https://example.com/foo") is None

    # Indeed CTS tracker — opaque blob fallback.
    cts = "https://cts.indeed.com/v3/H4sIAAAAAAAA_42RzW6bQBRG3wWprELMgPmxJVS5tkAm"
    jk = _extract_indeed_jk(cts)
    assert jk is not None and jk.startswith("cts_")

    assert _extract_li_job_id("https://www.linkedin.com/jobs/view/12345/") == "12345"
    assert _extract_li_job_id("https://www.linkedin.com/comm/jobs/view/67890/?x=y") == "67890"
    assert _extract_li_job_id("https://www.linkedin.com/?currentJobId=11111") == "11111"

    assert (
        _extract_zr_id("https://www.ziprecruiter.com/jobs/foo-bar/Abc123XyZ")
        == "Abc123XyZ"
    )
    # ZipRecruiter email tracker tokens.
    assert (
        _extract_zr_id("https://www.ziprecruiter.com/km/AAHq2ryj6HJ-cmDasT4yKkIjaXctZZWf")
        == "AAHq2ryj6HJ-cmDasT4yKkIjaXctZZWf"
    )
    assert (
        _extract_zr_id("https://www.ziprecruiter.com/ekm/AAEiGy6UNl_DWuknJ0YuPS7YjQRa71Qb")
        == "AAEiGy6UNl_DWuknJ0YuPS7YjQRa71Qb"
    )


def test_email_digest_title_from_subject():
    from lister.discovery.email_digest import _title_from_subject

    # Clean "Title @ Company" subjects → extract the title cleanly.
    assert _title_from_subject("Fwd: Customer Service Manager @ Clever Real Estate") == "Customer Service Manager"
    assert _title_from_subject("Re: Fwd: Underwriter II at Acme Insurance") == "Underwriter II"
    assert _title_from_subject("Fwd: Customer Service Representative opening at Afni") == "Customer Service Representative"
    assert _title_from_subject("Fwd: $34/hr Payroll Specialist job in Tallahassee") == "$34/hr Payroll Specialist"

    # Generic-marketing subjects → return empty so the caller mines the body.
    assert _title_from_subject("Fwd: I think this job might be right for you!") == ""
    assert _title_from_subject("Fwd: Ed, I think this job might be right for you!") == ""
    assert _title_from_subject("Fwd: Ed, Catalis has an open position") == ""
    assert _title_from_subject("Fwd: We think you'd be a great fit") == ""
    assert _title_from_subject("Fwd: I'm interested in you for my B2B Sales Rep position at Slice") == ""


def test_cookie_editor_conversion():
    """Cookie-Editor export array converts to Playwright cookie dicts."""
    from lister.browser import _convert_cookie_list

    raw = [
        {
            "name": "session",
            "value": "abc123",
            "domain": ".ziprecruiter.com",
            "path": "/",
            "secure": True,
            "httpOnly": True,
            "sameSite": "no_restriction",
            "expirationDate": 1799999999.5,
        },
        {
            "name": "csrf",
            "value": "xyz",
            "domain": "www.ziprecruiter.com",
            "sameSite": "lax",
        },
        {"junk": "no name or domain"},  # dropped
    ]
    out = _convert_cookie_list(raw)
    assert len(out) == 2
    assert out[0]["name"] == "session"
    assert out[0]["sameSite"] == "None"  # no_restriction -> None
    assert out[0]["expires"] == 1799999999.5
    assert out[1]["sameSite"] == "Lax"
    assert out[1]["path"] == "/"  # default
    assert out[1]["expires"] == -1  # session cookie default


def test_cookie_import_writes_storage_state(tmp_path, monkeypatch):
    import json as _json
    from lister import browser as _browser

    monkeypatch.setattr(_browser, "PROFILES_DIR", tmp_path)
    src = tmp_path / "zr-cookies.json"
    src.write_text(_json.dumps([
        {"name": "s", "value": "v", "domain": ".ziprecruiter.com", "path": "/"},
    ]))
    n = _browser.import_cookies_file("ziprecruiter", src)
    assert n == 1
    written = _json.loads((tmp_path / "ziprecruiter-storage.json").read_text())
    assert written["cookies"][0]["name"] == "s"
    assert "origins" in written
    """Body extraction picks role-keyword-rich lines over generic short lines."""
    from lister.discovery.email_digest import _extract_title_near

    ctx = (
        "We think you'd love this:\n"
        "Customer Service Manager\n"
        "Acme Insurance\n"
        "Tallahassee, FL\n"
        "Apply now\n"
    )
    assert _extract_title_near(ctx, fallback="x") == "Customer Service Manager"

    # When no role-keyword line exists, falls back to whatever it has.
    ctx2 = "Open position\nSee details\nApply\n"
    assert _extract_title_near(ctx2, fallback="fallback") == "fallback"


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
