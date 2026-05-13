"""SQLite-backed state. Tracks jobs we've seen, scored, and applied to."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from .config import REPO_ROOT
from .models import Application, ApplicationStatus, Job, MatchScore

DEFAULT_DB_PATH = REPO_ROOT / "data" / "lister.sqlite"


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id TEXT PRIMARY KEY,
    platform TEXT NOT NULL,
    platform_id TEXT NOT NULL,
    company TEXT NOT NULL,
    title TEXT NOT NULL,
    location TEXT,
    remote INTEGER NOT NULL DEFAULT 0,
    apply_url TEXT NOT NULL,
    listing_url TEXT NOT NULL,
    salary_min_usd INTEGER,
    salary_max_usd INTEGER,
    posted_at TEXT,
    discovered_at TEXT NOT NULL,
    description_text TEXT NOT NULL,
    raw_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_platform ON jobs(platform);
CREATE INDEX IF NOT EXISTS idx_jobs_discovered_at ON jobs(discovered_at);

CREATE TABLE IF NOT EXISTS scores (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    score INTEGER NOT NULL,
    reasons_json TEXT NOT NULL,
    dealbreakers_json TEXT NOT NULL,
    scored_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS applications (
    job_id TEXT PRIMARY KEY REFERENCES jobs(id),
    status TEXT NOT NULL,
    score INTEGER,
    resume_path TEXT,
    cover_letter_path TEXT,
    error TEXT,
    submitted_at TEXT,
    notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_apps_status ON applications(status);
CREATE INDEX IF NOT EXISTS idx_apps_submitted ON applications(submitted_at);
"""


@contextmanager
def connect(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    p = db_path or DEFAULT_DB_PATH
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_job(conn: sqlite3.Connection, job: Job) -> bool:
    """Insert a job if new. Returns True if inserted, False if already seen."""
    cur = conn.execute("SELECT 1 FROM jobs WHERE id = ?", (job.id,))
    if cur.fetchone():
        return False
    conn.execute(
        """
        INSERT INTO jobs (
            id, platform, platform_id, company, title, location, remote,
            apply_url, listing_url, salary_min_usd, salary_max_usd,
            posted_at, discovered_at, description_text, raw_json
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            job.id,
            job.platform.value,
            job.platform_id,
            job.company,
            job.title,
            job.location,
            1 if job.remote else 0,
            job.apply_url,
            job.listing_url,
            job.salary_min_usd,
            job.salary_max_usd,
            job.posted_at.isoformat() if job.posted_at else None,
            job.discovered_at.isoformat(),
            job.description_text,
            json.dumps(job.raw) if job.raw else None,
        ),
    )
    return True


def save_score(conn: sqlite3.Connection, score: MatchScore) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO scores (job_id, score, reasons_json, dealbreakers_json, scored_at)
        VALUES (?,?,?,?,?)
        """,
        (
            score.job_id,
            score.score,
            json.dumps(score.reasons),
            json.dumps(score.dealbreakers),
            datetime.utcnow().isoformat(),
        ),
    )


def submissions_today(conn: sqlite3.Connection) -> int:
    today = datetime.utcnow().date().isoformat()
    cur = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = ? AND substr(submitted_at, 1, 10) = ?",
        (ApplicationStatus.SUBMITTED.value, today),
    )
    return cur.fetchone()[0]


def save_application(conn: sqlite3.Connection, app: Application) -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO applications
            (job_id, status, score, resume_path, cover_letter_path, error, submitted_at, notes)
        VALUES (?,?,?,?,?,?,?,?)
        """,
        (
            app.job_id,
            app.status.value,
            app.score,
            app.resume_path,
            app.cover_letter_path,
            app.error,
            app.submitted_at.isoformat() if app.submitted_at else None,
            app.notes,
        ),
    )
