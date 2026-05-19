"""Discover jobs by reading job-alert emails out of Gmail via IMAP.

This connector is the fast path while we build the bigger browser-based
scraper for full job-board coverage. Workflow:

1. User configures Gmail credentials (an app password) in .env.
2. User subscribes to job alerts on Indeed / ZipRecruiter / LinkedIn / etc.
3. This connector reads recent emails from those senders and parses out the
   jobs they contain.

Per-sender parsers extract title, company, location and the canonical
job URL. The platform field on the resulting Job is set to the underlying
job-source (Indeed, ZipRecruiter, LinkedIn) so the application layer can
later route to the right submitter regardless of how we discovered the
posting.
"""
from __future__ import annotations

import email
import email.policy
import imaplib
import logging
import os
import re
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta
from urllib.parse import parse_qs, urlparse

from bs4 import BeautifulSoup

from ..config import Criteria
from ..models import Job, Platform
from .base import Connector

log = logging.getLogger(__name__)

GMAIL_HOST = "imap.gmail.com"
GMAIL_PORT = 993

DEFAULT_SENDERS = (
    "alert@indeed.com",
    "noreply@ziprecruiter.com",
    "jobs-noreply@linkedin.com",
    "jobalerts-noreply@linkedin.com",
)


class EmailDigestConnector(Connector):
    platform_name = "email_digest"

    def discover(self, criteria: Criteria) -> Iterable[Job]:
        cfg = criteria.platforms.email_digest
        if not cfg.enabled:
            return

        user = os.environ.get("GMAIL_USER", "").strip()
        password = os.environ.get("GMAIL_PASSWORD", "").strip()
        if not user or not password:
            log.error(
                "email_digest: GMAIL_USER and GMAIL_PASSWORD must both be set in .env. "
                "See docs for app-password setup."
            )
            return

        direct = list(cfg.senders) if cfg.senders else list(DEFAULT_SENDERS)
        forwarded = list(cfg.forwarded_from or [])
        # Preserve order, drop duplicates if a sender appears in both lists.
        senders = list(dict.fromkeys(direct + forwarded))
        since = datetime.utcnow() - timedelta(days=cfg.max_age_days)

        yielded = 0
        try:
            with _imap_connection(user, password) as conn:
                for sender in senders:
                    kind = _sender_kind(sender) or "forwarded"
                    msgs = list(_fetch_recent_from(conn, sender, since))
                    log.info(
                        "email_digest: %s (%s) → %d messages since %s",
                        sender,
                        kind,
                        len(msgs),
                        since.date().isoformat(),
                    )
                    for msg in msgs:
                        # Run every parser against every email. Each parser
                        # only yields when its platform-specific URL pattern
                        # matches, so the right one fires regardless of who
                        # sent the email — works for both direct alerts and
                        # forwarded mail from a personal inbox.
                        for parser_name, parser_fn in PARSERS.items():
                            try:
                                for job in parser_fn(msg):
                                    if yielded >= cfg.max_jobs:
                                        log.info(
                                            "email_digest: max_jobs cap (%d) hit — stopping",
                                            cfg.max_jobs,
                                        )
                                        return
                                    yielded += 1
                                    yield job
                            except Exception as e:
                                log.warning(
                                    "email_digest: %s parser failed on %s msg: %s",
                                    parser_name,
                                    kind,
                                    e,
                                )
        except imaplib.IMAP4.error as e:
            log.error(
                "email_digest: IMAP login/select failed: %s. "
                "Confirm: (1) 2-Step Verification is on, (2) you're using a 16-char "
                "app password (not your regular password), (3) IMAP is enabled in "
                "Gmail settings.",
                e,
            )
        except Exception as e:
            log.error("email_digest: unexpected error: %s", e)


# ---------- IMAP plumbing ----------


@contextmanager
def _imap_connection(user: str, password: str):
    conn = imaplib.IMAP4_SSL(GMAIL_HOST, GMAIL_PORT)
    try:
        conn.login(user, password)
        conn.select("INBOX", readonly=True)
        yield conn
    finally:
        try:
            conn.close()
        except Exception:
            pass
        try:
            conn.logout()
        except Exception:
            pass


def _fetch_recent_from(conn: imaplib.IMAP4_SSL, sender: str, since_dt: datetime) -> Iterator:
    date_str = since_dt.strftime("%d-%b-%Y")
    typ, data = conn.search(None, f'(FROM "{sender}" SINCE {date_str})')
    if typ != "OK" or not data or not data[0]:
        return
    for msgid in data[0].split():
        typ, msg_data = conn.fetch(msgid, "(RFC822)")
        if typ != "OK" or not msg_data or msg_data[0] is None:
            continue
        raw = msg_data[0][1]
        if not isinstance(raw, (bytes, bytearray)):
            continue
        yield email.message_from_bytes(bytes(raw), policy=email.policy.default)


def _sender_kind(addr: str) -> str | None:
    a = addr.lower()
    if "indeed.com" in a:
        return "indeed"
    if "ziprecruiter.com" in a:
        return "ziprecruiter"
    if "linkedin.com" in a:
        return "linkedin"
    return None


# ---------- email body helpers ----------


def _email_body_html(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                try:
                    return part.get_content()
                except Exception:
                    pass
        return ""
    if msg.get_content_type() == "text/html":
        try:
            return msg.get_content()
        except Exception:
            return ""
    return ""


def _email_body_text(msg) -> str:
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    return part.get_content()
                except Exception:
                    pass
    elif msg.get_content_type() == "text/plain":
        try:
            return msg.get_content()
        except Exception:
            return ""
    # Fall back to stripping HTML.
    html = _email_body_html(msg)
    if html:
        return BeautifulSoup(html, "lxml").get_text("\n", strip=True)
    return ""


# ---------- per-sender parsers ----------


def parse_indeed(msg) -> Iterator[Job]:
    subject = msg.get("Subject", "") or ""
    urls_with_ctx = _collect_links(msg)
    indeed_urls = [(href, text, ctx) for href, text, ctx in urls_with_ctx if "indeed.com" in href.lower()]
    log.debug("indeed parser: %d total / %d indeed urls in %r",
              len(urls_with_ctx), len(indeed_urls), subject[:80])
    for href, _, ctx in indeed_urls[:5]:
        log.debug("  indeed url sample: %s", href[:200])

    seen: set[str] = set()
    for href, anchor_text, ctx in indeed_urls:
        jk = _extract_indeed_jk(href)
        if not jk or jk in seen:
            continue
        title = anchor_text or _extract_title_near(ctx, fallback=f"Indeed job {jk}")
        if _is_junk_title(title):
            continue
        seen.add(jk)
        company = _guess_company(ctx, title)
        location = _guess_location(ctx)
        canonical = f"https://www.indeed.com/viewjob?jk={jk}"
        yield Job(
            id=f"indeed:email:{jk}",
            platform=Platform.INDEED,
            platform_id=jk,
            company=company or "",
            title=title,
            location=location,
            remote=_looks_remote(location, ctx),
            description_text=ctx[:2000],
            description_html=None,
            apply_url=canonical,
            listing_url=canonical,
            posted_at=None,
            raw={"sender": "indeed", "subject": subject},
        )


def parse_ziprecruiter(msg) -> Iterator[Job]:
    subject = msg.get("Subject", "") or ""
    urls_with_ctx = _collect_links(msg)
    zr_urls = [(href, text, ctx) for href, text, ctx in urls_with_ctx if "ziprecruiter" in href.lower()]
    log.debug("zr parser: %d total / %d zr urls in %r",
              len(urls_with_ctx), len(zr_urls), subject[:80])
    for href, _, ctx in zr_urls[:5]:
        log.debug("  zr url sample: %s", href[:200])

    seen: set[str] = set()
    for href, anchor_text, ctx in zr_urls:
        zr_id = _extract_zr_id(href)
        if not zr_id or zr_id in seen:
            continue
        title = anchor_text or _extract_title_near(ctx, fallback=f"ZipRecruiter job {zr_id}")
        if _is_junk_title(title):
            continue
        seen.add(zr_id)
        company = _guess_company(ctx, title)
        location = _guess_location(ctx)
        yield Job(
            id=f"ziprecruiter:email:{zr_id}",
            platform=Platform.ZIPRECRUITER,
            platform_id=zr_id,
            company=company or "",
            title=title,
            location=location,
            remote=_looks_remote(location, ctx),
            description_text=ctx[:2000],
            description_html=None,
            apply_url=href,
            listing_url=href,
            posted_at=None,
            raw={"sender": "ziprecruiter", "subject": subject},
        )


def parse_linkedin(msg) -> Iterator[Job]:
    subject = msg.get("Subject", "") or ""
    urls_with_ctx = _collect_links(msg)
    li_urls = [(href, text, ctx) for href, text, ctx in urls_with_ctx if "linkedin.com" in href.lower()]
    log.debug("linkedin parser: %d total / %d linkedin urls in %r",
              len(urls_with_ctx), len(li_urls), subject[:80])

    seen: set[str] = set()
    for href, anchor_text, ctx in li_urls:
        li_id = _extract_li_job_id(href)
        if not li_id or li_id in seen:
            continue
        title = anchor_text or _extract_title_near(ctx, fallback=f"LinkedIn job {li_id}")
        if _is_junk_title(title):
            continue
        seen.add(li_id)
        company = _guess_company(ctx, title)
        location = _guess_location(ctx)
        canonical = f"https://www.linkedin.com/jobs/view/{li_id}/"
        yield Job(
            id=f"linkedin:email:{li_id}",
            platform=Platform.LINKEDIN,
            platform_id=li_id,
            company=company or "",
            title=title,
            location=location,
            remote=_looks_remote(location, ctx),
            description_text=ctx[:2000],
            description_html=None,
            apply_url=canonical,
            listing_url=canonical,
            posted_at=None,
            raw={"sender": "linkedin", "subject": subject},
        )


PARSERS = {
    "indeed": parse_indeed,
    "ziprecruiter": parse_ziprecruiter,
    "linkedin": parse_linkedin,
}


# ---------- URL extractors ----------


def _extract_indeed_jk(href: str) -> str | None:
    """Find an Indeed job key in an href.

    Handles direct URLs (?jk=abc), tracking-redirect URLs that URL-encode the
    target (jk%3Dabc), and inline-text URL appearances.
    """
    if "indeed.com" not in href.lower():
        return None
    # 1. Direct top-level query param.
    try:
        qs = parse_qs(urlparse(href).query)
        jk = qs.get("jk", [None])[0]
        if jk:
            return jk
    except Exception:
        pass
    # 2. jk= anywhere in the string (inline appearance).
    m = re.search(r"[?&]jk=([A-Za-z0-9_-]{8,})", href)
    if m:
        return m.group(1)
    # 3. URL-encoded inner URL — common in mailing-list tracker wrappers.
    m = re.search(r"jk%3D([A-Za-z0-9_-]{8,})", href)
    if m:
        return m.group(1)
    return None


_ZR_PATTERNS = (
    re.compile(r"ziprecruiter\.com/jobs/[^/?#&]+/([A-Za-z0-9_-]{8,})"),
    re.compile(r"ziprecruiter\.com/c/[^/]+/Job/[^/?#&]+-([A-Za-z0-9_-]{8,})"),
    re.compile(r"[?&]lvk=([A-Za-z0-9_-]+)"),
    # URL-encoded variants (tracking wrappers)
    re.compile(r"ziprecruiter\.com%2Fjobs%2F[^%]+%2F([A-Za-z0-9_-]{8,})"),
    re.compile(r"lvk%3D([A-Za-z0-9_-]+)"),
)


def _extract_zr_id(href: str) -> str | None:
    if "ziprecruiter" not in href.lower():
        return None
    for pat in _ZR_PATTERNS:
        m = pat.search(href)
        if m:
            return m.group(1)
    return None


def _extract_li_job_id(href: str) -> str | None:
    if "linkedin.com" not in href.lower():
        return None
    # Direct path.
    m = re.search(r"/jobs/view/(\d+)", href)
    if m:
        return m.group(1)
    # URL-encoded.
    m = re.search(r"%2Fjobs%2Fview%2F(\d+)", href)
    if m:
        return m.group(1)
    # currentJobId query param.
    try:
        qs = parse_qs(urlparse(href).query)
        jid = qs.get("currentJobId", [None])[0]
        if jid and jid.isdigit():
            return jid
    except Exception:
        pass
    return None


def _collect_links(msg) -> list[tuple[str, str, str]]:
    """Collect (href, anchor_text, context) tuples from a message.

    Scans both the HTML body (preferred — gives us anchor text + context)
    and the plain-text body (fallback — emits URLs with empty anchor text).
    """
    out: list[tuple[str, str, str]] = []
    html = _email_body_html(msg)
    if html:
        soup = BeautifulSoup(html, "lxml")
        for a in soup.find_all("a", href=True):
            href = a["href"].strip()
            if not href or href.startswith(("mailto:", "tel:", "#")):
                continue
            anchor_text = (a.get_text(strip=True) or a.get("aria-label", "") or "").strip()
            if len(anchor_text) > 200:
                anchor_text = ""
            ctx = _surrounding_context(a)
            out.append((href, anchor_text, ctx))
    text = _email_body_text(msg)
    if text:
        seen_hrefs = {h for h, _, _ in out}
        for m in re.finditer(r"https?://[^\s<>\"'\)\]]+", text):
            href = m.group(0).strip(".,;)")
            if href in seen_hrefs:
                continue
            # Best-effort context: 200 chars around the URL in the text.
            start = max(0, m.start() - 200)
            end = min(len(text), m.end() + 200)
            ctx = text[start:end]
            out.append((href, "", ctx))
            seen_hrefs.add(href)
    return out


_JUNK_TITLE_PHRASES = (
    "view all",
    "view jobs",
    "view in browser",
    "view this email",
    "unsubscribe",
    "manage preferences",
    "manage subscriptions",
    "manage your alerts",
    "manage alerts",
    "see all",
    "see more",
    "open in",
    "click here",
    "edit your",
    "update your",
)


def _is_junk_title(title: str) -> bool:
    if not title or len(title) < 5 or len(title) > 200:
        return True
    lo = title.lower()
    return any(p in lo for p in _JUNK_TITLE_PHRASES)


def _extract_title_near(ctx: str, fallback: str) -> str:
    """Best-effort: pull the first plausible-looking job-title line from context."""
    if not ctx:
        return fallback
    for line in ctx.split("\n"):
        line = line.strip()
        if 10 <= len(line) <= 120 and not line.startswith(("http", "View ", "Apply", "Click")):
            return line
    return fallback


# ---------- context heuristics ----------


def _surrounding_context(elem, max_chars: int = 600) -> str:
    """Grab text around an element to help with company/location guessing."""
    parent = elem.parent
    for _ in range(4):
        if parent is None:
            break
        text = parent.get_text(" ", strip=True)
        if len(text) > 60:
            return text[:max_chars]
        parent = parent.parent
    return ""


_LOCATION_PATTERNS = (
    re.compile(r"\bRemote\s+in\s+(?:the\s+)?[A-Za-z][A-Za-z .]+", re.IGNORECASE),
    re.compile(r"\bRemote\b", re.IGNORECASE),
    re.compile(r"\b[A-Z][a-zA-Z]+(?:\s[A-Z][a-zA-Z]+)?,\s*[A-Z]{2}\b"),
)


def _guess_location(ctx: str) -> str | None:
    for pat in _LOCATION_PATTERNS:
        m = pat.search(ctx)
        if m:
            return m.group(0).strip()
    return None


def _guess_company(ctx: str, title: str) -> str:
    """Heuristic: company is usually the chunk right after the title."""
    if not ctx:
        return ""
    remaining = ctx.replace(title, "", 1).strip()
    # Try common separators.
    for sep in ("·", "—", "–", "|"):
        if sep in remaining:
            chunks = [c.strip() for c in remaining.split(sep) if c.strip()]
            for c in chunks:
                if _looks_like_company(c):
                    return c[:80]
    # Otherwise take the first reasonable-looking token sequence.
    for token in re.split(r"[\n\r]+|\s\s+", remaining):
        token = token.strip()
        if _looks_like_company(token):
            return token[:80]
    return ""


def _looks_like_company(s: str) -> bool:
    if not s or len(s) < 2 or len(s) > 80:
        return False
    if any(skip in s.lower() for skip in ("apply", "view", "remote", "unsubscribe", "$", "hour")):
        return False
    # Reject location-shaped strings
    if re.match(r"^[A-Z][a-zA-Z]+,\s*[A-Z]{2}$", s):
        return False
    return True


def _looks_remote(location: str | None, text: str) -> bool:
    blob = f"{location or ''} {text[:500]}".lower()
    return "remote" in blob
