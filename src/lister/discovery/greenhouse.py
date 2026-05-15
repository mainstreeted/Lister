"""Greenhouse discovery via the public board-API.

Each company exposes its board at:
    https://boards-api.greenhouse.io/v1/boards/<slug>/jobs?content=true

This is a public, documented endpoint — no auth, no scraping required. Greenhouse
explicitly hosts this for job seekers.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import datetime

import httpx
from bs4 import BeautifulSoup
from tenacity import retry, retry_if_exception, stop_after_attempt, wait_exponential

from ..config import Criteria
from ..models import Job, Platform
from .base import Connector

log = logging.getLogger(__name__)

API_TMPL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true"


def _is_transient_http_error(exc: BaseException) -> bool:
    """Retry on network blips and 5xx — never on 4xx (won't get better)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code >= 500
    return isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.ReadError))


class GreenhouseConnector(Connector):
    platform_name = "greenhouse"

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client or httpx.Client(
            timeout=30.0,
            headers={
                "User-Agent": "Lister/0.1 (personal job search automation)",
                "Accept": "application/json",
            },
        )

    def discover(self, criteria: Criteria) -> Iterable[Job]:
        cfg = criteria.platforms.greenhouse
        if not cfg.enabled:
            return
        for slug in cfg.companies:
            try:
                yield from self._fetch_company(slug, criteria)
            except Exception as e:
                log.warning("greenhouse: failed to fetch %s: %s", slug, e)

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=10),
        retry=retry_if_exception(_is_transient_http_error),
    )
    def _get(self, url: str) -> dict:
        r = self.client.get(url)
        r.raise_for_status()
        return r.json()

    def _fetch_company(self, slug: str, criteria: Criteria) -> Iterable[Job]:
        data = self._get(API_TMPL.format(slug=slug))
        for raw in data.get("jobs", []):
            job = self._normalize(slug, raw)
            if job is None:
                continue
            if not self._passes_coarse_filter(job, criteria):
                continue
            yield job

    def _normalize(self, slug: str, raw: dict) -> Job | None:
        gh_id = str(raw.get("id"))
        if not gh_id:
            return None

        title = (raw.get("title") or "").strip()
        location = (raw.get("location") or {}).get("name") or None
        listing_url = raw.get("absolute_url") or ""
        # GH applies happen on the same URL (with the apply form embedded).
        apply_url = listing_url

        # Description comes as escaped HTML.
        html = raw.get("content") or ""
        text = _html_to_text(html)

        posted_at = _parse_dt(raw.get("updated_at") or raw.get("first_published"))

        return Job(
            id=f"greenhouse:{slug}:{gh_id}",
            platform=Platform.GREENHOUSE,
            platform_id=gh_id,
            company=_company_name_from_slug(slug, raw),
            title=title,
            location=location,
            remote=_looks_remote(location, text),
            description_text=text,
            description_html=html,
            apply_url=apply_url,
            listing_url=listing_url,
            posted_at=posted_at,
            raw=raw,
        )

    def _passes_coarse_filter(self, job: Job, c: Criteria) -> bool:
        """Cheap pre-filter.

        We deliberately do NOT filter on title here. ATS title strings drift
        wildly across companies (e.g. "Manager, Customer Operations" vs.
        "Operations Manager" vs. "Operations Lead") and a string-match filter
        drops obvious hits. The ranker — keyword or LLM — decides title fit.

        We DO filter on:
          - excluded companies
          - location (remote_ok or city match)
          - max age
        """
        if c.filters.exclude_companies and job.company in c.filters.exclude_companies:
            return False

        if c.filters.locations:
            if job.remote and c.filters.remote_ok:
                pass
            elif job.location and any(
                loc.lower() in job.location.lower() for loc in c.filters.locations
            ):
                pass
            else:
                return False

        if c.filters.max_age_days and job.posted_at:
            age_days = (datetime.utcnow() - job.posted_at).days
            if age_days > c.filters.max_age_days:
                return False

        return True


def _html_to_text(html: str) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "lxml")
    # Drop scripts/styles, keep readable text.
    for tag in soup(["script", "style"]):
        tag.decompose()
    return soup.get_text("\n", strip=True)


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        # GH uses ISO 8601 with timezone; strip tz for naive UTC storage.
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _looks_remote(location: str | None, text: str) -> bool:
    blob = f"{location or ''} {text[:1000]}".lower()
    return "remote" in blob


def _company_name_from_slug(slug: str, raw: dict) -> str:
    # GH job payloads don't always include the company name. The slug is usually it.
    departments = raw.get("departments") or []
    if departments:
        # No company field — fall back to slug.
        pass
    return slug.replace("-", " ").title()
