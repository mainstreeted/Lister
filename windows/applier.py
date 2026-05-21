"""Lister — Windows macro applier.

Runs on Ed's Windows machine, ideally overnight via Task Scheduler. It reads
the apply-queue the WSL "brain" exported, then drives Ed's real, everyday
Chrome with real OS mouse/keyboard input to apply to each job. Results are
written back for `lister import-results` to ingest.

Why this design (see ../HANDOFF.md for the full rationale): sites like
ZipRecruiter gate launched/automated browsers behind Cloudflare. A real
browser that has earned trust over months is never challenged. Trust cannot
be manufactured, only inherited — so the automation runs *inside* that real
browser, using real input events, while Ed is asleep.

Usage (Windows, from this directory):
    python applier.py                 # uses config.json
    python applier.py --dry-run       # force dry run regardless of the queue
    python applier.py --limit 1       # process at most 1 job

Setup and scheduling: see README.md.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote_plus

import applyqueue as q
import chrome
from locate import Locator

SCRIPT_DIR = Path(__file__).resolve().parent
log = logging.getLogger("applier")


# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------

class Config:
    """Applier settings, loaded from config.json (see config.example.json)."""

    def __init__(self, data: dict):
        self.queue_file = data.get("queue_file", "")
        self.results_file = data.get("results_file", "")
        self.log_dir = data.get("log_dir", "logs")
        self.claude_cli_path = data.get("claude_cli_path", "claude")
        self.failsafe = bool(data.get("failsafe", True))
        self.page_load_seconds = tuple(data.get("page_load_seconds", [5, 9]))
        self.confirm_wait_seconds = tuple(data.get("confirm_wait_seconds", [3, 6]))
        self.between_jobs_seconds = tuple(data.get("between_jobs_seconds", [20, 45]))

    @classmethod
    def load(cls, path: Path) -> "Config":
        if path.exists():
            return cls(json.loads(path.read_text(encoding="utf-8")))
        log.warning("No config at %s — using defaults (queue/results must be "
                    "passed on the command line).", path)
        return cls({})


# ----------------------------------------------------------------------
# Apply flow
# ----------------------------------------------------------------------

_APPLY_BUTTON = (
    "the job-application call-to-action button, labelled '1-Click Apply', "
    "'Quick Apply', or 'Apply Now'"
)


def _save_shot(image, shot_dir: Path, job: q.Job, label: str) -> None:
    """Save a debug screenshot so Ed can review what the macro saw at 4am."""
    safe = re.sub(r"[^A-Za-z0-9]+", "-", job.job_id)[:40]
    out = shot_dir / f"{safe}--{label}.png"
    try:
        image.save(out)
    except Exception as e:
        log.warning("could not save screenshot %s: %s", out, e)


def _search_fallback(job: q.Job, loc: Locator, cfg: Config,
                     shot_dir: Path) -> tuple[int, int] | None:
    """ZR /km/ tracking links expire. Reach a live posting via ZR search.

    Navigates to the search page, opens the best-matching result, and returns
    the Apply-button coordinates on that job page (or None).
    """
    if not job.search_query:
        return None
    url = (
        "https://www.ziprecruiter.com/jobs-search?search="
        + quote_plus(job.search_query)
    )
    log.info("[%s] search fallback → %s", job.job_id, url)
    chrome.navigate(url)
    chrome.pause(*cfg.page_load_seconds)

    img = chrome.screenshot()
    _save_shot(img, shot_dir, job, "01b-search")
    result = loc.find(
        img,
        f"the first job posting card in the search results list — prefer one "
        f"titled like {job.title!r} at the company {job.company!r}",
    )
    if result is None:
        return None
    chrome.human_click(*result)
    chrome.pause(*cfg.page_load_seconds)

    img = chrome.screenshot()
    _save_shot(img, shot_dir, job, "01c-search-job")
    return loc.find(img, _APPLY_BUTTON)


def apply_ziprecruiter(job: q.Job, dry_run: bool, loc: Locator, cfg: Config,
                       shot_dir: Path) -> q.Result:
    chrome.new_tab()
    log.info("[%s] navigate → %s", job.job_id, job.nav_url[:120])
    chrome.navigate(job.nav_url)
    chrome.pause(*cfg.page_load_seconds)
    chrome.scroll(-600)  # nudge lazy-loaded CTAs into view

    img = chrome.screenshot()
    _save_shot(img, shot_dir, job, "01-landed")

    coords = loc.find(img, _APPLY_BUTTON)
    if coords is None:
        log.info("[%s] no Apply button on the listing — trying search", job.job_id)
        coords = _search_fallback(job, loc, cfg, shot_dir)
        if coords is None:
            chrome.close_tab()
            return q.Result(
                job.job_id, "failed", "apply_button_not_found",
                "No Apply button on the listing page or via ZipRecruiter "
                "search. The posting may be expired or off-platform.",
            )

    if dry_run:
        chrome.human_move(*coords)
        _save_shot(chrome.screenshot(), shot_dir, job, "02-would-click")
        chrome.close_tab()
        log.info("[%s] DRY RUN — hovering Apply, not clicking", job.job_id)
        return q.Result(
            job.job_id, "dry_run", "dry_run",
            f"Would have clicked Apply at {coords}.",
        )

    log.info("[%s] clicking Apply at %s", job.job_id, coords)
    chrome.human_click(*coords)
    chrome.pause(*cfg.confirm_wait_seconds)

    after = chrome.screenshot()
    _save_shot(after, shot_dir, job, "03-after-click")

    if loc.find(
        after,
        "text confirming the job application was sent or submitted "
        "successfully, such as 'Application submitted' or 'Thanks for applying'",
    ):
        chrome.close_tab()
        return q.Result(job.job_id, "submitted", "submitted",
                        "Confirmation message visible after clicking Apply.")

    if loc.find(
        after,
        "a multi-step application form asking extra questions, or a prompt to "
        "upload a resume or create an account",
    ):
        chrome.close_tab()
        return q.Result(
            job.job_id, "skipped", "complex_form",
            "Clicked Apply but a multi-step form appeared; the v1 applier "
            "does not fill these.",
        )

    chrome.close_tab()
    return q.Result(
        job.job_id, "failed", "no_confirmation",
        "Clicked Apply but no confirmation was visible. Worth checking "
        "ZipRecruiter manually.",
    )


def apply_job(job: q.Job, dry_run: bool, loc: Locator, cfg: Config,
              shot_dir: Path) -> q.Result:
    if job.platform == "ziprecruiter":
        return apply_ziprecruiter(job, dry_run, loc, cfg, shot_dir)
    return q.Result(
        job.job_id, "skipped", "platform_not_supported",
        f"The v1 Windows applier only handles ZipRecruiter; got "
        f"{job.platform!r}. Apply to this one by hand for now.",
    )


# ----------------------------------------------------------------------
# Entry point
# ----------------------------------------------------------------------

def _setup_logging(log_dir: Path) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    logfile = log_dir / f"applier-{stamp}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(logfile, encoding="utf-8"),
                  logging.StreamHandler(sys.stdout)],
    )
    return logfile


def main() -> int:
    ap = argparse.ArgumentParser(description="Lister Windows macro applier.")
    ap.add_argument("--config", type=Path, default=SCRIPT_DIR / "config.json")
    ap.add_argument("--queue", type=Path, help="Override queue_file from config.")
    ap.add_argument("--results", type=Path, help="Override results_file from config.")
    ap.add_argument("--dry-run", action="store_true",
                    help="Force dry run even if the queue asks for real submission.")
    ap.add_argument("--limit", type=int, default=0,
                    help="Process at most N jobs (0 = no cap).")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    log_dir = Path(cfg.log_dir)
    if not log_dir.is_absolute():
        log_dir = SCRIPT_DIR / log_dir
    logfile = _setup_logging(log_dir)
    log.info("Lister Windows applier starting — log: %s", logfile)

    queue_file = args.queue or (Path(cfg.queue_file) if cfg.queue_file else None)
    results_file = args.results or (
        Path(cfg.results_file) if cfg.results_file else None
    )
    if not queue_file or not results_file:
        log.error("queue_file and results_file must be set (config.json or "
                  "--queue/--results). See config.example.json.")
        return 2
    if not queue_file.exists():
        log.error("Queue file not found: %s. Run `lister export-queue` in WSL "
                  "first.", queue_file)
        return 2

    try:
        jobs, queue_dry_run = q.read_queue(queue_file)
    except (ValueError, KeyError, json.JSONDecodeError) as e:
        log.error("Could not read queue %s: %s", queue_file, e)
        return 2

    if not jobs:
        log.info("Queue is empty — nothing to do.")
        q.write_results(results_file, [])
        return 0

    if args.limit > 0:
        jobs = jobs[: args.limit]

    # The queue's dry_run is authoritative; --dry-run can only make it safer,
    # never force a real submission the WSL side did not ask for.
    dry_run = queue_dry_run or args.dry_run
    log.info("%d job(s) to process. dry_run=%s%s", len(jobs), dry_run,
             " (forced by --dry-run)" if args.dry_run and not queue_dry_run else "")

    chrome.configure(failsafe=cfg.failsafe)
    loc = Locator(claude_cli_path=cfg.claude_cli_path)

    shot_dir = log_dir / "screens" / datetime.now().strftime("%Y%m%d-%H%M%S")
    shot_dir.mkdir(parents=True, exist_ok=True)

    try:
        title = chrome.focus_chrome()
        log.info("Focused Chrome window: %r", title)
    except RuntimeError as e:
        log.error("%s", e)
        return 1

    results: list[q.Result] = []
    for i, job in enumerate(jobs, 1):
        log.info("=== [%d/%d] %s — %r @ %r ===",
                 i, len(jobs), job.job_id, job.title, job.company)
        try:
            result = apply_job(job, dry_run, loc, cfg, shot_dir)
        except Exception as e:  # never let one job kill the whole run
            log.exception("[%s] applier crashed: %s", job.job_id, e)
            try:
                chrome.close_tab()
            except Exception:
                pass
            result = q.Result(job.job_id, "failed", "applier_crashed",
                              str(e)[:400])
        log.info("[%s] → %s (%s)", job.job_id, result.status, result.reason)
        results.append(result)

        if i < len(jobs):
            chrome.pause(*cfg.between_jobs_seconds)

    q.write_results(results_file, results)
    log.info("Wrote %d result(s) to %s", len(results), results_file)

    tally: dict[str, int] = {}
    for r in results:
        tally[r.status] = tally.get(r.status, 0) + 1
    log.info("Summary: %s", "  ".join(f"{k}={v}" for k, v in sorted(tally.items())))
    log.info("Screenshots: %s", shot_dir)
    log.info("Back in WSL, run: lister import-results")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        sys.exit(130)
