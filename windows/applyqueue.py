"""Apply-queue file I/O for the Windows applier.

Standalone (no third-party deps) so it runs under a bare Windows Python.
Mirrors the schema documented in ``src/lister/queue_io.py`` on the WSL side.
The two runtimes share nothing but this JSON contract.
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

SCHEMA_VERSION = 1


class Job:
    """One job to apply to, as read from apply-queue.json."""

    def __init__(self, raw: dict):
        self.job_id: str = raw["job_id"]
        self.platform: str = raw.get("platform", "")
        self.company: str = raw.get("company", "")
        self.title: str = raw.get("title", "")
        self.score = raw.get("score")
        self.apply_url: str = raw.get("apply_url", "")
        self.listing_url: str = raw.get("listing_url", "")
        self.search_query: str = raw.get("search_query", "")

    @property
    def nav_url(self) -> str:
        """Best URL to navigate to first — the human-readable listing page."""
        return self.listing_url or self.apply_url

    def __repr__(self) -> str:
        return f"<Job {self.job_id} {self.title!r} @ {self.company!r}>"


def read_queue(path: Path) -> tuple[list[Job], bool]:
    """Read apply-queue.json. Returns (jobs, dry_run)."""
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    schema = payload.get("schema")
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"Queue schema {schema!r} != expected {SCHEMA_VERSION}. "
            "The WSL brain and this applier are out of sync."
        )
    jobs = [Job(j) for j in payload.get("jobs", [])]
    return jobs, bool(payload.get("dry_run", True))


class Result:
    """Outcome of one application attempt, written to apply-results.json."""

    def __init__(self, job_id: str, status: str, reason: str, notes: str = ""):
        self.job_id = job_id
        self.status = status  # submitted | dry_run | failed | skipped
        self.reason = reason
        self.notes = notes

    def as_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "reason": self.reason,
            "notes": self.notes,
        }


def write_results(path: Path, results: list[Result]) -> None:
    """Write apply-results.json for `lister import-results` to ingest."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA_VERSION,
        "completed_at": datetime.now().isoformat(timespec="seconds"),
        "results": [r.as_dict() for r in results],
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
