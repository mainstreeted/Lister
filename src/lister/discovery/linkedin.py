"""LinkedIn discovery via a logged-in browser session.

Strategy:
- For each search query in criteria.platforms.linkedin.search_queries, fetch
  the LinkedIn jobs search page with the remote work-type filter and a 30-day
  recency filter.
- Scrape job IDs from the rendered list, then visit each detail page to extract
  the structured JobPosting JSON-LD that LinkedIn embeds. JSON-LD is far more
  stable than DOM selectors.

Requires a one-time login via ``lister login linkedin``.
"""
from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterable
from datetime import datetime
from urllib.parse import quote_plus

from bs4 import BeautifulSoup

from ..browser import browser_context, goto_with_retry, human_pause
from ..config import Criteria
from ..models import Job, Platform
from .base import Connector

log = logging.getLogger(__name__)

JOBS_PER_QUERY = 25
DETAIL_URL_TMPL = "https://www.linkedin.com/jobs/view/{job_id}/"
SEARCH_URL_TMPL = (
    "https://www.linkedin.com/jobs/search/"
    "?keywords={kw}"
    "&f_WT=2"          # 2 = Remote
    "&f_TPR=r2592000"  # past 30 days
    "&sortBy=DD"       # Date Descending
)


class LinkedInConnector(Connector):
    platform_name = "linkedin"

    def __init__(self, jobs_per_query: int = JOBS_PER_QUERY) -> None:
        self.jobs_per_query = jobs_per_query

    def discover(self, criteria: Criteria) -> Iterable[Job]:
        cfg = criteria.platforms.linkedin
        if not cfg.enabled:
            return

        with browser_context("linkedin", headless=True) as ctx:
            page = ctx.new_page()

            if not self._is_logged_in(page):
                log.error(
                    "LinkedIn session not authenticated. "
                    "Run: lister login linkedin"
                )
                return

            for query in cfg.search_queries:
                try:
                    job_ids = self._search(page, query)
                    log.info("linkedin: %r → %d job ids", query, len(job_ids))
                except Exception as e:
                    log.warning("linkedin: search failed for %r: %s", query, e)
                    continue

                for jid in job_ids[: self.jobs_per_query]:
                    try:
                        job = self._fetch_detail(page, jid)
                        if job and self._passes_coarse_filter(job, criteria):
                            yield job
                    except Exception as e:
                        log.warning("linkedin: detail fetch failed for %s: %s", jid, e)
                    human_pause()

    # ---------- auth ----------

    def _is_logged_in(self, page) -> bool:
        try:
            goto_with_retry(page, "https://www.linkedin.com/feed/")
            url = page.url
            return "/login" not in url and "/uas/" not in url and "/authwall" not in url
        except Exception as e:
            log.warning("linkedin: login check error: %s", e)
            return False

    # ---------- search ----------

    def _search(self, page, query: str) -> list[str]:
        url = SEARCH_URL_TMPL.format(kw=quote_plus(query))
        goto_with_retry(page, url)
        human_pause(0.8, 1.4)

        # LinkedIn lazy-loads cards on scroll. A few scrolls catch most of page 1.
        for _ in range(4):
            page.mouse.wheel(0, 2500)
            human_pause(0.25, 0.6)

        html = page.content()
        # Job IDs surface in multiple formats; collect all matches and dedupe.
        ids = set(re.findall(r"jobPosting:(\d+)", html))
        ids |= set(re.findall(r"/jobs/view/(\d+)", html))
        return sorted(ids, reverse=True)  # newest IDs first

    # ---------- detail ----------

    def _fetch_detail(self, page, job_id: str) -> Job | None:
        url = DETAIL_URL_TMPL.format(job_id=job_id)
        goto_with_retry(page, url)
        human_pause(0.4, 1.0)

        html = page.content()
        data = _parse_jsonld(html)
        if not data:
            return None

        title = (data.get("title") or "").strip()
        description_html = data.get("description") or ""
        description_text = BeautifulSoup(description_html, "lxml").get_text("\n", strip=True)
        company = ((data.get("hiringOrganization") or {}).get("name") or "").strip()
        location = _parse_location(data.get("jobLocation"))
        is_remote = _looks_remote(data, location, description_text)

        posted_at = _parse_dt(data.get("datePosted"))

        return Job(
            id=f"linkedin:{job_id}",
            platform=Platform.LINKEDIN,
            platform_id=job_id,
            company=company,
            title=title,
            location=location,
            remote=is_remote,
            description_text=description_text,
            description_html=description_html,
            apply_url=url,
            listing_url=url,
            posted_at=posted_at,
            raw=data,
        )

    # ---------- filtering ----------

    def _passes_coarse_filter(self, job: Job, c: Criteria) -> bool:
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


# ---------- helpers (module-level so they can be unit-tested) ----------


def _parse_jsonld(html: str) -> dict | None:
    """Extract the JobPosting JSON-LD object from a LinkedIn detail page."""
    soup = BeautifulSoup(html, "lxml")
    for tag in soup.find_all("script", type="application/ld+json"):
        raw = tag.string or tag.get_text() or ""
        if not raw.strip():
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue
        found = _find_job_posting(data)
        if found:
            return found
    return None


def _find_job_posting(data) -> dict | None:
    if isinstance(data, dict):
        if data.get("@type") == "JobPosting":
            return data
        graph = data.get("@graph") or []
        for item in graph:
            found = _find_job_posting(item)
            if found:
                return found
    elif isinstance(data, list):
        for item in data:
            found = _find_job_posting(item)
            if found:
                return found
    return None


def _parse_location(loc) -> str | None:
    """JobLocation can be a string, dict, or list. Flatten to a string."""
    if not loc:
        return None
    if isinstance(loc, str):
        return loc.strip() or None
    if isinstance(loc, list):
        parts = [_parse_location(x) for x in loc]
        joined = "; ".join(p for p in parts if p)
        return joined or None
    if isinstance(loc, dict):
        addr = loc.get("address") or {}
        if isinstance(addr, str):
            return addr
        city = (addr.get("addressLocality") or "").strip()
        region = (addr.get("addressRegion") or "").strip()
        country = (addr.get("addressCountry") or "").strip()
        joined = ", ".join(p for p in [city, region, country] if p)
        return joined or None
    return None


def _looks_remote(data: dict, location: str | None, text: str) -> bool:
    if data.get("jobLocationType", "").upper() == "TELECOMMUTE":
        return True
    blob = f"{location or ''} {text[:500]}".lower()
    return "remote" in blob


def _parse_dt(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None
