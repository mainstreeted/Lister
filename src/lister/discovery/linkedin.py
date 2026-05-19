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
            auth_page = ctx.new_page()
            auth_page.set_default_navigation_timeout(20000)
            authed = self._is_logged_in(auth_page)
            auth_page.close()
            if not authed:
                log.error(
                    "LinkedIn session not authenticated. "
                    "Run: lister login linkedin"
                )
                return

            for query in cfg.search_queries:
                # Fresh page per query — isolates failures and stops stale
                # state from one query polluting the next.
                page = ctx.new_page()
                page.set_default_navigation_timeout(20000)
                page.set_default_timeout(20000)

                try:
                    job_ids = self._search(page, query)
                    log.info("linkedin: %r → %d job ids", query, len(job_ids))
                    yielded = 0
                except Exception as e:
                    log.error(
                        "linkedin: search failed for %r: %s. "
                        "Browser may be dead — aborting remaining LinkedIn queries.",
                        query,
                        e,
                    )
                    try:
                        page.close()
                    except Exception:
                        pass
                    return  # short-circuit; don't waste minutes retrying a dead browser

                for jid in job_ids[: self.jobs_per_query]:
                    try:
                        job = self._fetch_detail(page, jid)
                        if not job:
                            log.debug(
                                "linkedin: jid=%s dropped — _fetch_detail returned None "
                                "(likely no JSON-LD on detail page)",
                                jid,
                            )
                        elif not self._passes_coarse_filter(job, criteria):
                            log.debug(
                                "linkedin: jid=%s filtered out  title=%r  location=%r  remote=%s",
                                jid,
                                job.title,
                                job.location,
                                job.remote,
                            )
                        else:
                            yielded += 1
                            yield job
                    except Exception as e:
                        log.warning("linkedin: detail fetch failed for %s: %s", jid, e)
                    human_pause()

                log.info(
                    "linkedin: query %r → %d of %d job ids passed filters",
                    query,
                    yielded,
                    len(job_ids),
                )

                try:
                    page.close()
                except Exception:
                    pass

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

    # CSS selectors used to find rendered job cards. LinkedIn ships at least three
    # different list layouts; try them all.
    JOB_CARD_SELECTORS = (
        "[data-occludable-job-id]",
        "li[data-job-id]",
        "ul.jobs-search__results-list li",
        ".scaffold-layout__list li",
    )

    def _search(self, page, query: str) -> list[str]:
        url = SEARCH_URL_TMPL.format(kw=quote_plus(query))
        goto_with_retry(page, url)

        # Wait for the actual results list to render. If LinkedIn shows a
        # bot-detection wall, this selector never appears and we fall through
        # to the empty case below (which saves a screenshot).
        rendered = False
        for selector in self.JOB_CARD_SELECTORS:
            try:
                page.wait_for_selector(selector, timeout=8000)
                rendered = True
                break
            except Exception:
                continue
        if not rendered:
            log.warning(
                "linkedin: no recognizable results list for %r — page may be a "
                "bot-wall or LinkedIn changed its layout",
                query,
            )

        human_pause(0.6, 1.0)

        # Scroll the jobs list container itself (not the page body) — this is
        # what triggers LinkedIn's lazy loader.
        for _ in range(5):
            page.evaluate(
                """() => {
                    const list = document.querySelector(
                        '.jobs-search__results-list, .scaffold-layout__list, '
                        + '.jobs-search-results-list, [data-test-paginated-list]'
                    );
                    if (list) list.scrollTop = list.scrollHeight;
                    else window.scrollBy(0, 1500);
                }"""
            )
            human_pause(0.4, 0.9)

        # Extract IDs from rendered DOM elements first (most reliable).
        ids: list[str] = []
        for selector in self.JOB_CARD_SELECTORS:
            try:
                attr_ids = page.eval_on_selector_all(
                    selector,
                    """els => els.map(e =>
                        e.getAttribute('data-occludable-job-id')
                        || e.getAttribute('data-job-id')
                        || (e.querySelector('a[href*=\"/jobs/view/\"]') || {})
                              .getAttribute && (e.querySelector('a[href*=\"/jobs/view/\"]').getAttribute('href')||'').match(/jobs\\/view\\/(\\d+)/)?.[1]
                    ).filter(Boolean)""",
                )
                if attr_ids:
                    ids.extend(attr_ids)
                    break
            except Exception as e:
                log.debug("linkedin: selector %s failed: %s", selector, e)

        # Fallback: raw-HTML regex (catches SEO-baked links too — noisy but
        # better than nothing if the DOM extraction missed something).
        if not ids:
            html = page.content()
            ids = list(set(re.findall(r"/jobs/view/(\d+)", html)))

        # Dedupe, preserve order, drop empties.
        seen = set()
        unique: list[str] = []
        for x in ids:
            x = str(x).strip()
            if x and x not in seen:
                seen.add(x)
                unique.append(x)

        if not unique:
            self._save_debug_screenshot(page, query)

        return unique

    def _save_debug_screenshot(self, page, query: str) -> None:
        """Dump a screenshot when a search returns nothing so we can see why."""
        try:
            from ..config import REPO_ROOT

            slug = re.sub(r"[^a-z0-9]+", "-", query.lower())[:40]
            out = REPO_ROOT / "data" / f"linkedin-debug-{slug}.png"
            out.parent.mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(out), full_page=False)
            log.warning("linkedin: 0 results for %r — screenshot saved to %s", query, out)
        except Exception as e:
            log.warning("linkedin: failed to save debug screenshot: %s", e)

    # ---------- detail ----------

    def _fetch_detail(self, page, job_id: str) -> Job | None:
        url = DETAIL_URL_TMPL.format(job_id=job_id)
        goto_with_retry(page, url)

        # JSON-LD is sometimes injected slightly after domcontentloaded; wait
        # briefly for the script tag to appear before scraping.
        try:
            page.wait_for_selector(
                'script[type="application/ld+json"]', timeout=5000
            )
        except Exception:
            pass
        human_pause(0.4, 1.0)

        html = page.content()
        data = _parse_jsonld(html)
        if not data:
            has_ld_marker = "application/ld+json" in html
            log.debug(
                "linkedin: jid=%s no JobPosting found  html=%d bytes  ld+json_in_page=%s",
                job_id,
                len(html),
                has_ld_marker,
            )
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
