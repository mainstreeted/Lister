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

        senders = list(cfg.senders) if cfg.senders else list(DEFAULT_SENDERS)
        since = datetime.utcnow() - timedelta(days=cfg.max_age_days)

        yielded = 0
        try:
            with _imap_connection(user, password) as conn:
                for sender in senders:
                    kind = _sender_kind(sender)
                    parser = PARSERS.get(kind)
                    if parser is None:
                        log.warning("email_digest: no parser for sender %r — skipping", sender)
                        continue
                    msgs = list(_fetch_recent_from(conn, sender, since))
                    log.info(
                        "email_digest: %s (%s) → %d messages since %s",
                        sender,
                        kind,
                        len(msgs),
                        since.date().isoformat(),
                    )
                    for msg in msgs:
                        try:
                            for job in parser(msg):
                                if yielded >= cfg.max_jobs:
                                    log.info(
                                        "email_digest: max_jobs cap (%d) hit — stopping",
                                        cfg.max_jobs,
                                    )
                                    return
                                yielded += 1
                                yield job
                        except Exception as e:
                            log.warning("email_digest: %s message parse failed: %s", kind, e)
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
    html = _email_body_html(msg)
    if not html:
        return
    soup = BeautifulSoup(html, "lxml")
    subject = msg.get("Subject", "") or ""
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        jk = _extract_indeed_jk(href)
        if not jk or jk in seen:
            continue
        title = (anchor.get_text(strip=True) or anchor.get("aria-label", "")).strip()
        # Skip junk anchors (logo links, "View all", footer, etc.)
        if not title or len(title) < 5 or len(title) > 200:
            continue
        if any(skip in title.lower() for skip in ("view all", "unsubscribe", "view in browser")):
            continue
        seen.add(jk)
        ctx = _surrounding_context(anchor)
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
    html = _email_body_html(msg)
    if not html:
        return
    soup = BeautifulSoup(html, "lxml")
    subject = msg.get("Subject", "") or ""
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        zr_id = _extract_zr_id(href)
        if not zr_id or zr_id in seen:
            continue
        title = (anchor.get_text(strip=True) or anchor.get("aria-label", "")).strip()
        if not title or len(title) < 5 or len(title) > 200:
            continue
        if any(skip in title.lower() for skip in ("view all", "unsubscribe", "manage", "preferences")):
            continue
        seen.add(zr_id)
        ctx = _surrounding_context(anchor)
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
    html = _email_body_html(msg)
    if not html:
        return
    soup = BeautifulSoup(html, "lxml")
    subject = msg.get("Subject", "") or ""
    seen: set[str] = set()
    for anchor in soup.find_all("a", href=True):
        href = anchor["href"]
        li_id = _extract_li_job_id(href)
        if not li_id or li_id in seen:
            continue
        title = (anchor.get_text(strip=True) or anchor.get("aria-label", "")).strip()
        if not title or len(title) < 5 or len(title) > 200:
            continue
        if any(skip in title.lower() for skip in ("view all", "unsubscribe", "see more")):
            continue
        seen.add(li_id)
        ctx = _surrounding_context(anchor)
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
    if "indeed.com" not in href:
        return None
    try:
        qs = parse_qs(urlparse(href).query)
        jk = qs.get("jk", [None])[0]
        return jk
    except Exception:
        return None


_ZR_PATTERNS = (
    re.compile(r"ziprecruiter\.com/jobs/[^/?#]+/([A-Za-z0-9_-]+)"),
    re.compile(r"ziprecruiter\.com/c/[^/]+/Job/[^/?#]+-([A-Za-z0-9_-]+)"),
    re.compile(r"[?&]lvk=([A-Za-z0-9_-]+)"),
)


def _extract_zr_id(href: str) -> str | None:
    if "ziprecruiter.com" not in href:
        return None
    for pat in _ZR_PATTERNS:
        m = pat.search(href)
        if m:
            return m.group(1)
    return None


def _extract_li_job_id(href: str) -> str | None:
    if "linkedin.com" not in href:
        return None
    m = re.search(r"/jobs/view/(\d+)", href)
    if m:
        return m.group(1)
    # Tracking links may have currentJobId=12345
    try:
        qs = parse_qs(urlparse(href).query)
        jid = qs.get("currentJobId", [None])[0]
        if jid and jid.isdigit():
            return jid
    except Exception:
        pass
    return None


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
