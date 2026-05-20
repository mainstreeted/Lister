"""ZipRecruiter Quick Apply / 1-Click Apply submitter.

Walks a logged-in browser session through the apply flow:
  1. Navigate to the job's apply URL (often a tracking-redirect URL — let it
     resolve to the real ziprecruiter.com/jobs/... page).
  2. Confirm we're logged in (otherwise abort with a clear reason).
  3. Locate the apply CTA. ZR shows multiple shapes:
       - "1-Click Apply" — instant, single button click.
       - "Quick Apply"   — single click on ZR-hosted form.
       - "Apply Now"     — usually opens a modal; sometimes redirects off-site.
  4. In dry-run mode (default), report what we *would* click and stop.
  5. In real mode, click, then wait for a confirmation indicator and report.

Every step writes a verbose log line so the user can trace the flow.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime

from playwright.sync_api import BrowserContext

from ..browser import goto_with_retry, human_pause
from ..config import Criteria
from ..models import ApplicationStatus, Job
from .base import SubmitResult, Submitter

log = logging.getLogger(__name__)


# Text patterns we try, in order, to find the apply button. ZipRecruiter
# changes button copy occasionally — adding more variants is safe.
APPLY_BUTTON_PATTERNS = (
    re.compile(r"^\s*1[-\s]?click apply\s*$", re.IGNORECASE),
    re.compile(r"^\s*quick apply\s*$", re.IGNORECASE),
    re.compile(r"^\s*apply now\s*$", re.IGNORECASE),
    re.compile(r"^\s*apply\s*$", re.IGNORECASE),
)

# Indicators that submission succeeded after clicking apply.
SUCCESS_PATTERNS = (
    re.compile(r"application\s+(was\s+)?(sent|submitted|received)", re.IGNORECASE),
    re.compile(r"thanks for applying", re.IGNORECASE),
    re.compile(r"you[\'’]ve applied", re.IGNORECASE),
    re.compile(r"successfully\s+applied", re.IGNORECASE),
)

# Indicators that we hit a multi-step form / external redirect rather than
# 1-click submit. We skip these for now and revisit when we add form-filling.
COMPLEX_FORM_PATTERNS = (
    re.compile(r"answer\s+a\s+few\s+questions", re.IGNORECASE),
    re.compile(r"continue\s+on\s+(the\s+)?employer", re.IGNORECASE),
    re.compile(r"upload\s+your\s+resume", re.IGNORECASE),
    re.compile(r"create\s+(an\s+)?account", re.IGNORECASE),
)


class ZipRecruiterSubmitter(Submitter):
    platform_name = "ziprecruiter"

    def submit(
        self,
        job: Job,
        criteria: Criteria,
        ctx: BrowserContext,
        *,
        dry_run: bool = True,
    ) -> SubmitResult:
        page = ctx.new_page()
        page.set_default_navigation_timeout(20000)
        page.set_default_timeout(15000)
        try:
            return self._submit_inner(page, job, dry_run=dry_run)
        finally:
            try:
                page.close()
            except Exception:
                pass

    def _submit_inner(self, page, job: Job, *, dry_run: bool) -> SubmitResult:
        url = job.apply_url or job.listing_url
        log.info("[ziprecruiter] [%s] navigate → %s", job.id, url[:120])
        try:
            goto_with_retry(page, url)
        except Exception as e:
            log.warning("[ziprecruiter] [%s] navigation failed: %s", job.id, e)
            return SubmitResult(
                ApplicationStatus.FAILED, "navigation_failed", str(e)[:200]
            )

        # Tracking URLs redirect; let the page settle before we look at it.
        human_pause(0.6, 1.2)
        final_url = page.url
        log.info("[ziprecruiter] [%s] landed on %s", job.id, final_url[:120])

        if not self._is_logged_in(page):
            log.warning("[ziprecruiter] [%s] not logged in — aborting", job.id)
            return SubmitResult(
                ApplicationStatus.FAILED,
                "not_logged_in",
                "Login required; run `lister login ziprecruiter`.",
            )

        # Already applied? ZR sometimes shows "Applied" status on the job page.
        if self._already_applied(page):
            log.info("[ziprecruiter] [%s] already applied (per page UI)", job.id)
            return SubmitResult(
                ApplicationStatus.SKIPPED,
                "already_applied",
                "Page shows job has already been applied to.",
            )

        # Complex forms / off-platform redirects: skip for v1.
        body_text_sample = self._body_text(page)[:4000]
        for pat in COMPLEX_FORM_PATTERNS:
            if pat.search(body_text_sample):
                log.info(
                    "[ziprecruiter] [%s] complex form detected (pattern %s) — skipping",
                    job.id,
                    pat.pattern,
                )
                return SubmitResult(
                    ApplicationStatus.SKIPPED,
                    "complex_form",
                    f"Multi-step / off-site application: {pat.pattern}",
                )

        button, button_text = self._find_apply_button(page)
        if button is None:
            log.warning("[ziprecruiter] [%s] no apply button found on page", job.id)
            return SubmitResult(
                ApplicationStatus.FAILED,
                "no_apply_button",
                "Could not locate an Apply button. Page may have changed shape.",
            )

        log.info(
            "[ziprecruiter] [%s] found apply button: %r",
            job.id,
            (button_text or "").strip()[:80],
        )

        if dry_run:
            log.info("[ziprecruiter] [%s] DRY RUN — not clicking", job.id)
            return SubmitResult(
                ApplicationStatus.DRY_RUN,
                "dry_run",
                f"Would have clicked: {button_text!r} on {final_url[:80]}",
            )

        log.info("[ziprecruiter] [%s] CLICKING apply button", job.id)
        try:
            button.click()
        except Exception as e:
            log.warning("[ziprecruiter] [%s] click failed: %s", job.id, e)
            return SubmitResult(
                ApplicationStatus.FAILED, "click_failed", str(e)[:200]
            )

        # Wait briefly for a confirmation, modal, or redirect.
        human_pause(1.0, 2.0)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass

        post_body = self._body_text(page)[:6000]
        for pat in SUCCESS_PATTERNS:
            if pat.search(post_body):
                log.info(
                    "[ziprecruiter] [%s] success indicator matched: %s",
                    job.id,
                    pat.pattern,
                )
                return SubmitResult(
                    ApplicationStatus.SUBMITTED,
                    "submitted",
                    f"Confirmation text matched /{pat.pattern}/.",
                )

        # If we got a complex-form prompt after click, the apply isn't done.
        for pat in COMPLEX_FORM_PATTERNS:
            if pat.search(post_body):
                log.info(
                    "[ziprecruiter] [%s] post-click form detected (%s) — skipping",
                    job.id,
                    pat.pattern,
                )
                return SubmitResult(
                    ApplicationStatus.SKIPPED,
                    "complex_form_after_click",
                    f"Clicked apply but hit multi-step form: {pat.pattern}",
                )

        log.warning(
            "[ziprecruiter] [%s] clicked but no confirmation matched — uncertain",
            job.id,
        )
        return SubmitResult(
            ApplicationStatus.FAILED,
            "no_confirmation",
            "Clicked apply but couldn't confirm submission from page text. "
            "Worth checking ZR manually.",
        )

    # ---------- helpers ----------

    def _is_logged_in(self, page) -> bool:
        """Heuristic: logged-in pages don't display a 'Sign In' / 'Log In' CTA."""
        try:
            body = self._body_text(page)
            if not body:
                return False
            # Signed-out pages have a top-right "Sign In" link.
            signed_out_markers = (
                r"\bSign\s*in\b",
                r"\bLog\s*in\b",
                r"\bCreate\s+account\b",
            )
            for pat in signed_out_markers:
                if re.search(pat, body[:2000], re.IGNORECASE):
                    # Also look for a logged-in signal — if both present, trust login.
                    if re.search(r"\b(My Profile|My Jobs|Saved Jobs|Account)\b", body[:4000]):
                        return True
                    return False
            return True
        except Exception:
            return False

    def _already_applied(self, page) -> bool:
        body = self._body_text(page)
        return bool(
            re.search(r"\bApplied\b", body[:3000])
            and re.search(r"applied\s+on", body[:3000], re.IGNORECASE)
        )

    def _find_apply_button(self, page):
        """Return (locator_first, text) for the apply button, or (None, None)."""
        # First try button text; then anchor text. Most specific first.
        for pat in APPLY_BUTTON_PATTERNS:
            for role in ("button", "link"):
                try:
                    loc = page.get_by_role(role, name=pat)
                    if loc.count():
                        first = loc.first
                        try:
                            txt = first.inner_text(timeout=2000)
                        except Exception:
                            txt = ""
                        return first, txt
                except Exception:
                    continue
        # CSS fallbacks — ZR sometimes wraps the CTA in custom components.
        for sel in (
            'button[data-testid*="apply" i]',
            '[data-testid*="apply-button" i]',
            'a[href*="/apply"]',
        ):
            try:
                loc = page.locator(sel)
                if loc.count():
                    first = loc.first
                    try:
                        txt = first.inner_text(timeout=2000)
                    except Exception:
                        txt = ""
                    return first, txt
            except Exception:
                continue
        return None, None

    def _body_text(self, page) -> str:
        try:
            return page.locator("body").inner_text(timeout=3000)
        except Exception:
            return ""
