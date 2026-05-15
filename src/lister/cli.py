from __future__ import annotations

import logging
import shutil
from enum import Enum
from pathlib import Path

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

from . import db
from .config import load_criteria, load_resume
from .discovery import GreenhouseConnector
from .ranker import ApiRanker, ClaudeCliRanker, MockRanker, summarize_resume

load_dotenv()

app = typer.Typer(add_completion=False, help="Personal job application automation.")
console = Console()
log = logging.getLogger("lister")


class RankerChoice(str, Enum):
    AUTO = "auto"
    CLI = "cli"
    API = "api"
    MOCK = "mock"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _pick_ranker(choice: RankerChoice):
    """Resolve which ranker to use. `auto` picks claude CLI if available."""
    if choice == RankerChoice.MOCK:
        console.print("[dim]ranker: mock (keyword-only, no LLM)[/]")
        return MockRanker()
    if choice == RankerChoice.API:
        console.print("[dim]ranker: api (Anthropic SDK, requires ANTHROPIC_API_KEY)[/]")
        return ApiRanker()
    if choice == RankerChoice.CLI:
        console.print("[dim]ranker: claude CLI (uses your Claude Code subscription)[/]")
        return ClaudeCliRanker()
    # auto: prefer CLI if installed, else API if key set, else mock with a warning.
    if shutil.which("claude"):
        console.print("[dim]ranker: claude CLI (auto-selected; uses your Claude subscription)[/]")
        return ClaudeCliRanker()
    import os
    if os.environ.get("ANTHROPIC_API_KEY"):
        console.print("[dim]ranker: api (auto-selected; ANTHROPIC_API_KEY found)[/]")
        return ApiRanker()
    console.print(
        "[yellow]No `claude` CLI on PATH and no ANTHROPIC_API_KEY set — "
        "falling back to mock ranker. Install Claude Code or set an API key for real scoring.[/]"
    )
    return MockRanker()


@app.command()
def discover(
    criteria_path: Path = typer.Option(None, "--criteria", "-c", help="Path to criteria.yaml"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max jobs to display after ranking"),
    min_score: int | None = typer.Option(
        None, "--min-score", help="Override criteria.daily.min_match_score"
    ),
    rank: bool = typer.Option(True, "--rank/--no-rank", help="Run ranker on discovered jobs"),
    ranker_choice: RankerChoice = typer.Option(
        RankerChoice.AUTO,
        "--ranker",
        help="Which ranker to use: auto (prefer claude CLI), cli, api, or mock.",
        case_sensitive=False,
    ),
    mock_ranker: bool = typer.Option(
        False,
        "--mock-ranker",
        help="Shortcut for --ranker mock. Kept for backwards compatibility.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Discover and rank jobs from configured platforms. Writes results to the local DB."""
    _setup_logging(verbose)
    criteria = load_criteria(criteria_path)

    resume_summary = ""
    if rank:
        resume_md = load_resume(criteria.resumes.granular)
        resume_summary = summarize_resume(resume_md)

    connectors = []
    if criteria.platforms.greenhouse.enabled:
        connectors.append(GreenhouseConnector())
    # TODO: lever, ashby, workday, linkedin, indeed, ziprecruiter

    if not connectors:
        console.print("[yellow]No platforms enabled in criteria.yaml.[/]")
        raise typer.Exit(1)

    if rank:
        effective = RankerChoice.MOCK if mock_ranker else ranker_choice
        ranker = _pick_ranker(effective)
    else:
        ranker = None
    threshold = min_score if min_score is not None else criteria.daily.min_match_score

    # Phase 1: discover all jobs (no LLM calls).
    all_jobs: list = []
    discovered = 0
    new_jobs = 0
    with db.connect() as conn:
        for c in connectors:
            console.print(f"[cyan]Discovering via {c.platform_name}...[/]")
            for job in c.discover(criteria):
                discovered += 1
                inserted = db.upsert_job(conn, job)
                if inserted:
                    new_jobs += 1
                all_jobs.append(job)

    console.print(
        f"[green]Discovered {discovered} jobs ({new_jobs} new) "
        f"across {len(connectors)} platform(s).[/]"
    )

    if not ranker or not all_jobs:
        return

    # Phase 2: rank in one shot (batched if the ranker supports it).
    console.print(f"[cyan]Ranking {len(all_jobs)} jobs...[/]")
    scores = ranker.score_many(all_jobs, criteria, resume_summary)

    with db.connect() as conn:
        for s in scores:
            db.save_score(conn, s)

    pairs = list(zip(all_jobs, scores))
    pairs.sort(key=lambda x: x[1].score, reverse=True)
    top = pairs[:limit]

    table = Table(title=f"Top matches (>= {threshold} highlighted)")
    table.add_column("Score", justify="right", style="bold")
    table.add_column("Company")
    table.add_column("Title")
    table.add_column("Location")
    table.add_column("URL", overflow="fold")
    for job, score in top:
        style = "green" if score.score >= threshold else "white"
        table.add_row(
            f"[{style}]{score.score}[/]",
            job.company,
            job.title,
            job.location or ("Remote" if job.remote else ""),
            job.listing_url,
        )
    console.print(table)


@app.command()
def status() -> None:
    """Show recent applications and today's submission count."""
    with db.connect() as conn:
        today_count = db.submissions_today(conn)
        cur = conn.execute(
            "SELECT job_id, status, score, submitted_at FROM applications "
            "ORDER BY COALESCE(submitted_at, '') DESC LIMIT 20"
        )
        rows = cur.fetchall()
    console.print(f"[bold]Submissions today:[/] {today_count}")
    if not rows:
        console.print("[dim]No applications recorded yet.[/]")
        return
    table = Table(title="Recent applications")
    table.add_column("Job")
    table.add_column("Status")
    table.add_column("Score")
    table.add_column("Submitted")
    for r in rows:
        table.add_row(r["job_id"], r["status"], str(r["score"] or ""), r["submitted_at"] or "")
    console.print(table)


if __name__ == "__main__":
    app()
