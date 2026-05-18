"""Playwright-backed browser context for logged-in platform connectors.

Each platform gets a persistent user-data directory under ``.browser-profiles/``,
so cookies and session storage survive across runs. The first run per platform
is interactive (the user signs in by hand via ``lister login <platform>``);
subsequent runs are fully headless.
"""
from __future__ import annotations

import logging
import os
import random
import shutil
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from playwright.sync_api import BrowserContext, Page, sync_playwright

from .config import REPO_ROOT

log = logging.getLogger(__name__)

PROFILES_DIR = REPO_ROOT / ".browser-profiles"

# Reasonable, current desktop Chrome UA. Update if the platforms start fingerprinting
# old UA versions.
DEFAULT_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36"
)

LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-dev-shm-usage",
    "--disable-features=IsolateOrigins,site-per-process",
    # Stability flags for WSL / containerized Chrome — these prevent the most
    # common crashes when running headless Chrome on a system without a
    # proper GPU or with limited /dev/shm.
    "--disable-gpu",
    "--disable-software-rasterizer",
    "--disable-extensions",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-ipc-flooding-protection",
    "--no-first-run",
    "--no-default-browser-check",
]

# Default per-page timeouts. Pages that aren't responsive within this many
# milliseconds get aborted — keeps the run from hanging when the browser dies.
DEFAULT_NAV_TIMEOUT_MS = 20000

# Run on every new page to remove the most obvious automation tells.
STEALTH_INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
Object.defineProperty(navigator, 'plugins', {get: () => [1,2,3,4,5]});
window.chrome = window.chrome || {runtime: {}};
"""


@contextmanager
def browser_context(platform: str, headless: bool = True) -> Iterator[BrowserContext]:
    """Open a fresh Chromium context per call, hydrated with saved cookies.

    Architecture (changed from earlier prototype):
      - We no longer keep Chrome's whole persistent user-data-dir between runs
        — that directory grows large and aggressive writes to it on WSL's ext4
        can corrupt the filesystem.
      - Instead we save only cookies + localStorage to a small JSON file
        (``.browser-profiles/<platform>-storage.json``). Each run launches a
        clean ephemeral Chrome that loads that JSON, runs, writes it back.
      - Login stays sticky across runs; disk usage stays trivial.
    """
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    cookies_path = PROFILES_DIR / f"{platform}-storage.json"

    launch_kwargs: dict = dict(
        headless=headless,
        args=LAUNCH_ARGS,
    )
    exec_path = _find_system_chromium()
    if exec_path:
        log.info("browser: using system Chrome at %s", exec_path)
        launch_kwargs["executable_path"] = exec_path

    context_kwargs: dict = dict(
        viewport={"width": 1366, "height": 768},
        user_agent=DEFAULT_UA,
        locale="en-US",
    )
    if cookies_path.exists():
        context_kwargs["storage_state"] = str(cookies_path)
    else:
        log.info(
            "browser: no saved cookies for %s — login is interactive on first run",
            platform,
        )

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        try:
            context = browser.new_context(**context_kwargs)
            context.add_init_script(STEALTH_INIT_SCRIPT)
            context.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)
            try:
                yield context
                # Save cookies back so the next run is non-interactive.
                try:
                    context.storage_state(path=str(cookies_path))
                    log.debug("browser: saved storage state to %s", cookies_path)
                except Exception as e:
                    log.warning("browser: could not save storage state: %s", e)
            finally:
                context.close()
        finally:
            browser.close()


def _find_system_chromium() -> str | None:
    """Locate a system Chrome/Chromium binary, or None to use Playwright's bundle.

    Checked in order:
      1. ``LISTER_CHROME_PATH`` env var (explicit override)
      2. Common Debian/Ubuntu install paths for Google Chrome and Chromium
      3. ``which`` on the user's PATH
    """
    override = os.environ.get("LISTER_CHROME_PATH", "").strip()
    if override:
        if Path(override).exists():
            return override
        log.warning("LISTER_CHROME_PATH=%s does not exist; ignoring", override)
    for candidate in (
        "/usr/bin/google-chrome-stable",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium-browser",
        "/usr/bin/chromium",
        "/snap/bin/chromium",
    ):
        if Path(candidate).exists():
            return candidate
    return shutil.which("google-chrome") or shutil.which("chromium")


def human_pause(min_s: float = 0.3, max_s: float = 1.0) -> None:
    """Short random delay. Used between page loads to avoid request bursts.

    Tuned for efficiency, not paranoia — a human browsing 100 jobs in a session
    averages ~0.5s between clicks.
    """
    time.sleep(random.uniform(min_s, max_s))


def goto_with_retry(page: Page, url: str, max_attempts: int = 3, timeout_ms: int = 30000) -> None:
    """Navigate with a couple retries on transient failures."""
    last_err: Exception | None = None
    for attempt in range(max_attempts):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            return
        except Exception as e:
            last_err = e
            log.warning("nav attempt %d failed for %s: %s", attempt + 1, url, e)
            time.sleep(2**attempt)
    raise RuntimeError(f"failed to navigate to {url}: {last_err}")
