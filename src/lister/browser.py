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
    """Open a persistent Chromium context keyed to a platform name.

    The first time this is used per platform, the user logs in interactively
    via ``lister login <platform>`` (which passes ``headless=False``). Cookies
    are then stored at ``.browser-profiles/<platform>/`` and reused on every
    subsequent headless run.

    Uses a system-installed Chrome/Chromium if one is found (helpful when
    Playwright's bundled chromium doesn't support the host OS yet). Override
    with ``LISTER_CHROME_PATH=/path/to/binary``.
    """
    profile_dir = PROFILES_DIR / platform
    profile_dir.mkdir(parents=True, exist_ok=True)

    launch_kwargs: dict = dict(
        user_data_dir=str(profile_dir),
        headless=headless,
        args=LAUNCH_ARGS,
        viewport={"width": 1366, "height": 768},
        user_agent=DEFAULT_UA,
        locale="en-US",
    )
    exec_path = _find_system_chromium()
    if exec_path:
        log.info("browser: using system Chrome at %s", exec_path)
        launch_kwargs["executable_path"] = exec_path

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(**launch_kwargs)
        context.add_init_script(STEALTH_INIT_SCRIPT)
        try:
            yield context
        finally:
            context.close()


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
