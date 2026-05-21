"""Apply-queue file I/O — the WSL ↔ Windows boundary.

The WSL "brain" (discover / rank / DB) writes ``apply-queue.json``; the
Windows macro applier (``windows/applier.py``) reads it, drives Ed's real
Chrome, and writes ``apply-results.json`` back. ``lister import-results``
then ingests that file into the DB.

It is plain JSON on purpose: the two runtimes are separate Python installs
with no shared package, so they only need to agree on this schema.

apply-queue.json
----------------
{
  "schema": 1,
  "generated_at": "2026-05-21T08:00:00",
  "dry_run": true,
  "jobs": [
    {
      "job_id": "ziprecruiter:abc123",
      "platform": "ziprecruiter",
      "company": "Acme Insurance",
      "title": "Customer Service Representative",
      "score": 85,
      "apply_url": "https://www.ziprecruiter.com/...",
      "listing_url": "https://www.ziprecruiter.com/jobs/...",
      "search_query": "Customer Service Representative Acme Insurance"
    }
  ]
}

apply-results.json
------------------
{
  "schema": 1,
  "completed_at": "2026-05-21T04:03:11",
  "results": [
    {
      "job_id": "ziprecruiter:abc123",
      "status": "dry_run",          # one of ApplicationStatus values
      "reason": "dry_run",
      "notes": "Would have clicked '1-Click Apply'."
    }
  ]
}
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .config import REPO_ROOT

SCHEMA_VERSION = 1

DEFAULT_QUEUE_PATH = REPO_ROOT / "data" / "apply-queue.json"
DEFAULT_RESULTS_PATH = REPO_ROOT / "data" / "apply-results.json"


def write_queue(path: Path, jobs: list[dict], *, dry_run: bool) -> None:
    """Write the apply queue the Windows applier consumes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": SCHEMA_VERSION,
        "generated_at": datetime.utcnow().isoformat(timespec="seconds"),
        "dry_run": dry_run,
        "jobs": jobs,
    }
    path.write_text(json.dumps(payload, indent=2))


def read_results(path: Path) -> list[dict]:
    """Read the results file the Windows applier produced.

    Returns the list of per-job result dicts. Raises FileNotFoundError if the
    file is absent and ValueError if the schema version is unrecognized.
    """
    if not path.exists():
        raise FileNotFoundError(f"No results file at {path}")
    payload = json.loads(path.read_text())
    schema = payload.get("schema")
    if schema != SCHEMA_VERSION:
        raise ValueError(
            f"Results file schema {schema!r} != expected {SCHEMA_VERSION}. "
            "WSL brain and Windows applier are out of sync — update one side."
        )
    return list(payload.get("results", []))
