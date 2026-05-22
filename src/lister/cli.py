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
from .apply import ZipRecruiterSubmitter
from .config import load_criteria, load_resume
from .discovery import EmailDigestConnector, GreenhouseConnector, LinkedInConnector
from .models import Application, ApplicationStatus
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
    if criteria.platforms.linkedin.enabled:
        connectors.append(LinkedInConnector())
    if criteria.platforms.email_digest.enabled:
        connectors.append(EmailDigestConnector())
    # TODO: indeed, ziprecruiter (direct), lever, ashby, workday

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


LOGIN_TARGETS = {
    "linkedin": (
        "https://www.linkedin.com/login",
        "https://www.linkedin.com/feed/",
    ),
    "indeed": (
        "https://secure.indeed.com/account/login",
        "https://www.indeed.com/",
    ),
    "ziprecruiter": (
        "https://www.ziprecruiter.com/login",
        "https://www.ziprecruiter.com/jobs",
    ),
}


@app.command()
def login(
    platform: str = typer.Argument(
        ..., help="Platform to log in to: linkedin | indeed | ziprecruiter"
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """One-time interactive login. Opens a visible browser; you sign in by hand.

    Cookies persist under ``.browser-profiles/<platform>/`` so subsequent
    headless discovery runs reuse the same session — no re-auth needed.
    """
    _setup_logging(verbose)
    key = platform.lower().strip()
    if key not in LOGIN_TARGETS:
        console.print(
            f"[red]Unknown platform '{platform}'. Choose one of: "
            f"{', '.join(LOGIN_TARGETS)}[/]"
        )
        raise typer.Exit(1)

    login_url, verify_url = LOGIN_TARGETS[key]

    from .browser import browser_context, goto_with_retry

    console.print(f"[cyan]Opening a browser window for {key} login...[/]")
    console.print(
        f"[yellow]Log in to {key} in the window that appears. "
        "Once you see your home feed, come back to this terminal and press Enter.[/]"
    )

    with browser_context(key, headless=False) as ctx:
        page = ctx.new_page()
        try:
            goto_with_retry(page, login_url)
        except Exception as e:
            console.print(f"[red]Could not open {login_url}: {e}[/]")
            raise typer.Exit(1)

        input("Press Enter once you're logged in: ")

        try:
            goto_with_retry(page, verify_url)
        except Exception as e:
            console.print(f"[red]Verification navigation failed: {e}[/]")
            raise typer.Exit(1)

        if any(x in page.url for x in ("/login", "/uas/", "/authwall")):
            console.print(
                f"[red]Verification failed — still on a login/auth wall ({page.url}). "
                "Try again; make sure you complete any 2FA prompts.[/]"
            )
            raise typer.Exit(1)

    console.print(
        f"[green]✓ {key} session saved. "
        "Run `lister discover` and it will use this login automatically.[/]"
    )


SUBMITTERS = {
    "ziprecruiter": ZipRecruiterSubmitter,
    # "indeed": IndeedSubmitter,        # next session
    # "greenhouse": GreenhouseSubmitter, # next session
}


@app.command()
def apply(
    platform: str = typer.Option(
        "ziprecruiter",
        "--platform",
        help="Which platform's submitter to run. Currently only 'ziprecruiter' is wired.",
    ),
    limit: int = typer.Option(
        3,
        "--limit",
        "-n",
        help="Max applications to attempt this run (cap on top-ranked candidates).",
    ),
    min_score: int | None = typer.Option(
        None,
        "--min-score",
        help="Override the threshold from criteria.daily.min_match_score.",
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Default is DRY RUN — walks the form but does not click submit. "
        "Pass --no-dry-run to actually submit.",
    ),
    verbose: bool = typer.Option(False, "-v", "--verbose"),
) -> None:
    """Apply (or dry-run) to top-ranked, not-yet-applied jobs.

    Picks the highest-scoring jobs above the threshold that don't already
    have a non-failed entry in the applications table, opens a logged-in
    browser, and drives each through the platform's submitter. Records the
    outcome (submitted / dry_run / skipped / failed) in the DB.

    The default is DRY RUN. To send real applications, pass --no-dry-run.
    """
    _setup_logging(verbose)
    criteria = load_criteria()

    submitter_cls = SUBMITTERS.get(platform.lower())
    if submitter_cls is None:
        console.print(
            f"[red]No submitter for platform {platform!r}. "
            f"Available: {', '.join(SUBMITTERS)}.[/]"
        )
        raise typer.Exit(1)

    threshold = min_score if min_score is not None else criteria.daily.min_match_score
    console.print(
        f"[cyan]apply: platform={platform}  limit={limit}  "
        f"min_score={threshold}  dry_run={dry_run}[/]"
    )
    if dry_run:
        console.print(
            "[yellow]DRY RUN — no real applications will be submitted. "
            "Pass --no-dry-run to actually submit.[/]"
        )

    with db.connect() as conn:
        candidates = db.top_unapplied_jobs(
            conn, platform=platform.lower(), min_score=threshold, limit=limit
        )

    if not candidates:
        console.print(
            f"[yellow]No {platform} jobs scored ≥ {threshold} that haven't already "
            f"been applied to. Run `lister discover` to refresh.[/]"
        )
        return

    console.print(f"[green]Found {len(candidates)} candidate(s):[/]")
    preview = Table(show_header=True, header_style="bold")
    preview.add_column("Score", justify="right")
    preview.add_column("Company")
    preview.add_column("Title")
    preview.add_column("URL", overflow="fold", max_width=60)
    for c in candidates:
        preview.add_row(
            str(c["score"]),
            c["company"] or "—",
            (c["title"] or "")[:80],
            (c["apply_url"] or "")[:200],
        )
    console.print(preview)

    submitter = submitter_cls()

    from .browser import browser_context

    results: list[tuple[dict, "SubmitResult"]] = []  # type: ignore[name-defined]
    try:
        with browser_context(platform.lower(), headless=True) as ctx:
            for cand in candidates:
                console.print(
                    f"\n[bold cyan]>>> {cand['title'][:80]!r} at "
                    f"{cand['company'] or 'unknown'} (score {cand['score']})[/]"
                )
                job_id = cand["id"]
                # Build a Job-shaped dict for the submitter.
                from .models import Job, Platform

                job = Job(
                    id=cand["id"],
                    platform=Platform(cand["platform"]),
                    platform_id=cand["platform_id"],
                    company=cand["company"] or "",
                    title=cand["title"] or "",
                    location=cand["location"],
                    remote=bool(cand["remote"]),
                    description_text=cand["description_text"] or "",
                    apply_url=cand["apply_url"],
                    listing_url=cand["listing_url"],
                )
                try:
                    result = submitter.submit(job, criteria, ctx, dry_run=dry_run)
                except Exception as e:
                    log.exception("[%s] submitter crashed: %s", platform, e)
                    from .apply.base import SubmitResult

                    result = SubmitResult(
                        ApplicationStatus.FAILED,
                        "submitter_crashed",
                        str(e)[:500],
                    )

                console.print(
                    f"    → [{_color_for(result.status)}]{result.status.value}[/] "
                    f"({result.reason}) — {result.notes[:200]}"
                )

                # Record outcome.
                import datetime as _dt

                with db.connect() as conn:
                    db.save_application(
                        conn,
                        Application(
                            job_id=job_id,
                            status=result.status,
                            score=cand["score"],
                            submitted_at=(
                                _dt.datetime.utcnow() if result.success else None
                            ),
                            notes=result.notes or result.reason,
                            error=(result.notes if result.status == ApplicationStatus.FAILED else None),
                        ),
                    )
                results.append((cand, result))
    except Exception as e:
        log.exception("apply: fatal error in browser context: %s", e)
        console.print(f"[red]apply: fatal error: {e}[/]")
        raise typer.Exit(1)

    # Summary.
    submitted = sum(1 for _, r in results if r.status == ApplicationStatus.SUBMITTED)
    dryrun = sum(1 for _, r in results if r.status == ApplicationStatus.DRY_RUN)
    skipped = sum(1 for _, r in results if r.status == ApplicationStatus.SKIPPED)
    failed = sum(1 for _, r in results if r.status == ApplicationStatus.FAILED)
    console.print(
        f"\n[bold]Summary:[/] submitted={submitted}  dry_run={dryrun}  "
        f"skipped={skipped}  failed={failed}"
    )
    if dry_run and dryrun > 0:
        console.print(
            "[yellow]These were dry runs. Re-run with --no-dry-run to actually submit.[/]"
        )


def _color_for(status: ApplicationStatus) -> str:
    return {
        ApplicationStatus.SUBMITTED: "green",
        ApplicationStatus.DRY_RUN: "cyan",
        ApplicationStatus.SKIPPED: "yellow",
        ApplicationStatus.FAILED: "red",
    }.get(status, "white")


@app.command(name="import-cookies")
def import_cookies(
    platform: str = typer.Argument(
        ..., help="Platform the cookies belong to: linkedin | indeed | ziprecruiter"
    ),
    path: Path = typer.Argument(..., help="Path to the exported cookie JSON file."),
    keep_source: bool = typer.Option(
        False,
        "--keep-source",
        help="Keep the source cookie file after import. By default it's deleted "
        "(it contains live session tokens).",
    ),
) -> None:
    """Import browser cookies exported from a normal Chrome session.

    Use this when a platform's login page blocks automated browsers with a
    CAPTCHA / 'I am human' check. Log in normally in your everyday Chrome
    (where you pass the CAPTCHA as a human), export the site's cookies with a
    Cookie-Editor browser extension, and import them here. Lister's automation
    then reuses that authenticated session and never touches a login page.
    """
    _setup_logging(False)
    if not path.exists():
        console.print(f"[red]File not found: {path}[/]")
        raise typer.Exit(1)

    from .browser import PROFILES_DIR, import_cookies_file

    try:
        n = import_cookies_file(platform.lower(), path)
    except Exception as e:
        console.print(f"[red]Import failed: {e}[/]")
        raise typer.Exit(1)

    console.print(
        f"[green]✓ Imported {n} cookies for {platform.lower()}.[/] "
        f"Session saved to {PROFILES_DIR}/{platform.lower()}-storage.json"
    )
    if not keep_source:
        try:
            path.unlink()
            console.print(
                f"[dim]Deleted {path} (it held live session tokens). "
                "Pass --keep-source to retain it next time.[/]"
            )
        except Exception as e:
            console.print(
                f"[yellow]Imported OK, but couldn't delete {path}: {e}. "
                "Delete it yourself — it contains session tokens.[/]"
            )
    console.print(
        "[cyan]Next: run `lister apply --limit 3` (dry run) to test the session.[/]"
    )


@app.command(name="export-queue")
def export_queue(
    platform: str | None = typer.Option(
        None,
        "--platform",
        help="Limit the queue to one platform (e.g. ziprecruiter). Default: all.",
    ),
    limit: int = typer.Option(
        3, "--limit", "-n", help="Max jobs to put in the queue."
    ),
    min_score: int | None = typer.Option(
        None, "--min-score", help="Override criteria.daily.min_match_score."
    ),
    dry_run: bool = typer.Option(
        True,
        "--dry-run/--no-dry-run",
        help="Tells the Windows applier whether to actually submit. Default: dry run.",
    ),
    out: Path = typer.Option(
        None, "--out", help="Where to write the queue JSON. Default: data/apply-queue.json."
    ),
) -> None:
    """Export top-ranked, not-yet-applied jobs to a queue file for the Windows applier.

    The Windows-side macro (``windows/applier.py``) reads this file, drives
    Ed's real Chrome, and writes ``apply-results.json`` back — which
    ``lister import-results`` then ingests. See ``windows/README.md``.

    Exported jobs are marked ``queued`` in the DB so they aren't re-exported.
    """
    from . import queue_io

    criteria = load_criteria()
    threshold = min_score if min_score is not None else criteria.daily.min_match_score
    out_path = out or queue_io.DEFAULT_QUEUE_PATH

    with db.connect() as conn:
        candidates = db.top_unapplied_jobs(
            conn, platform=platform.lower() if platform else None,
            min_score=threshold, limit=limit,
        )
        if not candidates:
            console.print(
                f"[yellow]No jobs scored ≥ {threshold} that haven't been applied to. "
                "Run `lister discover` first.[/]"
            )
            raise typer.Exit(0)

        jobs = []
        for c in candidates:
            title = c["title"] or ""
            company = c["company"] or ""
            jobs.append(
                {
                    "job_id": c["id"],
                    "platform": c["platform"],
                    "company": company,
                    "title": title,
                    "score": c["score"],
                    "apply_url": c["apply_url"] or "",
                    "listing_url": c["listing_url"] or "",
                    "search_query": f"{title} {company}".strip(),
                }
            )

        # Mark them queued so a second export-queue run doesn't re-include them.
        for c in candidates:
            db.save_application(
                conn,
                Application(
                    job_id=c["id"],
                    status=ApplicationStatus.QUEUED,
                    score=c["score"],
                    notes="Exported to apply-queue for the Windows applier.",
                ),
            )

    queue_io.write_queue(out_path, jobs, dry_run=dry_run)
    console.print(
        f"[green]✓ Wrote {len(jobs)} job(s) to {out_path}[/]  "
        f"(dry_run={dry_run})"
    )
    console.print(
        "[cyan]Next: on Windows, run `python windows\\applier.py`, then back "
        "here run `lister import-results`.[/]"
    )


@app.command(name="import-results")
def import_results(
    path: Path = typer.Option(
        None,
        "--path",
        help="Results JSON from the Windows applier. Default: data/apply-results.json.",
    ),
) -> None:
    """Ingest the Windows applier's results file into the DB.

    Updates each job's application row with the outcome the macro reported
    (submitted / dry_run / failed / skipped).
    """
    import datetime as _dt

    from . import queue_io

    results_path = path or queue_io.DEFAULT_RESULTS_PATH
    try:
        results = queue_io.read_results(results_path)
    except (FileNotFoundError, ValueError) as e:
        console.print(f"[red]{e}[/]")
        raise typer.Exit(1)

    if not results:
        console.print(f"[yellow]No results in {results_path}.[/]")
        raise typer.Exit(0)

    counts: dict[str, int] = {}
    with db.connect() as conn:
        for r in results:
            job_id = r.get("job_id")
            if not job_id:
                continue
            try:
                status = ApplicationStatus(r.get("status", "failed"))
            except ValueError:
                status = ApplicationStatus.FAILED
            # Preserve the score recorded when the job was queued/scored.
            row = conn.execute(
                "SELECT score FROM scores WHERE job_id = ?", (job_id,)
            ).fetchone()
            score = row["score"] if row else None
            submitted = (
                _dt.datetime.utcnow()
                if status == ApplicationStatus.SUBMITTED
                else None
            )
            db.save_application(
                conn,
                Application(
                    job_id=job_id,
                    status=status,
                    score=score,
                    submitted_at=submitted,
                    notes=(r.get("notes") or r.get("reason") or "")[:500],
                    error=(
                        (r.get("notes") or r.get("reason"))
                        if status == ApplicationStatus.FAILED
                        else None
                    ),
                ),
            )
            counts[status.value] = counts.get(status.value, 0) + 1

    summary = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    console.print(f"[green]✓ Imported {len(results)} result(s):[/] {summary}")


if __name__ == "__main__":
    app()
