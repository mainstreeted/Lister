"""Submitter interface — one implementation per platform.

All submitters log every action verbosely (navigating, finding elements,
clicking, etc.) so the user can trace what the browser is doing step by step.
Dry-run is the default; real submission requires an explicit opt-in.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

from playwright.sync_api import BrowserContext

from ..config import Criteria
from ..models import ApplicationStatus, Job


@dataclass
class SubmitResult:
    """Outcome of a single application attempt.

    status: terminal status to record in the DB.
    reason: short machine-readable code ("dry_run", "no_apply_button", etc.).
    notes:  human-readable summary of what happened, written to the DB.
    """
    status: ApplicationStatus
    reason: str
    notes: str = ""

    @property
    def success(self) -> bool:
        return self.status in (ApplicationStatus.SUBMITTED, ApplicationStatus.DRY_RUN)


class Submitter(ABC):
    """Per-platform application submitter."""

    platform_name: str

    @abstractmethod
    def submit(
        self,
        job: Job,
        criteria: Criteria,
        ctx: BrowserContext,
        *,
        dry_run: bool = True,
    ) -> SubmitResult:
        """Apply to ``job`` using a logged-in browser context.

        When ``dry_run=True``, walk the form (navigate, locate fields, locate
        the submit button) and report what *would* happen, but do not actually
        click submit. This is the default — callers must opt into real
        submission explicitly.
        """
        ...
