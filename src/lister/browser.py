"""patchright-backed browser context for logged-in platform connectors.

We use `patchright` — a drop-in, detection-patched fork of Playwright — instead
of vanilla Playwright. Vanilla Playwright's Chromium fails Cloudflare's bot
challenge (the "Just a moment..." page) no matter what cookies it carries,
because Cloudflare fingerprints the *browser*, not the session. patchright
removes those automation fingerprints so the browser passes the challenge.

Per patchright's guidance for maximum stealth we:
  - use a persistent context (a real Chrome user-data dir), not an ephemeral one
  - drive the system Google Chrome (channel="chrome"), not bundled Chromium
  - do NOT inject manual stealth scripts or pass automation-hiding flags
    (patchright handles all of that; manual patches are themselves a tell)

Login cookies imported via `lister import-cookies` are layered into the
persistent context with add_cookies() at startup.
"""
from __future__ import annotations

import json
import logging
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# patchright re-exports Playwright's API surface; importing its sync_playwright
# gives us the detection-patched browser. Type names come from playwright
# (kept installed as a patchright dependency) and are runtime-compatible.
from patchright.sync_api import sync_playwright
from playwright.sync_api import BrowserContext, Page

from .config import REPO_ROOT

log = logging.getLogger(__name__)

PROFILES_DIR = REPO_ROOT / ".browser-profiles"

# Minimal launch args. We deliberately do NOT pass
# --disable-blink-features=AutomationControlled or similar — patchright handles
# automation-hiding, and passing those flags is itself a detectable signal.
# Only WSL/container stability flags remain.
LAUNCH_ARGS = [
    "--no-sandbox",
    "--disable-dev-shm-usage",
]

DEFAULT_NAV_TIMEOUT_MS = 25000


@contextmanager
def browser_context(platform: str, headless: bool = True) -> Iterator[BrowserContext]:
    """Open a persistent, detection-patched Chrome context for a platform.

    Cookies imported via ``lister import-cookies`` (stored at
    ``.browser-profiles/<platform>-storage.json``) are layered in at startup
    with add_cookies(). The persistent profile dir lives at
    ``.browser-profiles/<platform>-profile/``.
    """
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    profile_dir = PROFILES_DIR / f"{platform}-profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    cookies_path = PROFILES_DIR / f"{platform}-storage.json"

    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(profile_dir),
            channel="chrome",          # drive real Google Chrome
            headless=headless,
            args=LAUNCH_ARGS,
            no_viewport=True,
        )
        context.set_default_navigation_timeout(DEFAULT_NAV_TIMEOUT_MS)

        # Layer in imported login cookies, if present.
        if cookies_path.exists():
            try:
                data = json.loads(cookies_path.read_text())
                cookies = data.get("cookies", []) if isinstance(data, dict) else data
                cookies = _sanitize_cookies(cookies)
                if cookies:
                    context.add_cookies(cookies)
                    log.info(
                        "browser: loaded %d cookies for %s", len(cookies), platform
                    )
            except Exception as e:
                log.warning("browser: could not load cookies for %s: %s", platform, e)
        else:
            log.info(
                "browser: no imported cookies for %s — run `lister import-cookies %s <file>`",
                platform,
                platform,
            )

        try:
            yield context
            # Persist the (possibly refreshed) cookie set back to disk.
            try:
                state = context.storage_state()
                cookies_path.write_text(json.dumps(state, indent=2))
                log.debug("browser: saved storage state to %s", cookies_path)
            except Exception as e:
                log.warning("browser: could not save storage state: %s", e)
        finally:
            try:
                context.close()
            except Exception:
                pass


def _sanitize_cookies(cookies: list) -> list[dict]:
    """Keep only the fields Playwright's add_cookies accepts, and valid rows."""
    clean: list[dict] = []
    for c in cookies or []:
        if not isinstance(c, dict) or not c.get("name") or not c.get("domain"):
            continue
        row = {
            "name": c["name"],
            "value": c.get("value", ""),
            "domain": c["domain"],
            "path": c.get("path", "/"),
        }
        if isinstance(c.get("expires"), (int, float)) and c["expires"] > 0:
            row["expires"] = c["expires"]
        if "httpOnly" in c:
            row["httpOnly"] = bool(c["httpOnly"])
        if "secure" in c:
            row["secure"] = bool(c["secure"])
        if c.get("sameSite") in ("Lax", "Strict", "None"):
            row["sameSite"] = c["sameSite"]
        clean.append(row)
    return clean


def human_pause(min_s: float = 0.3, max_s: float = 1.0) -> None:
    """Short random delay. Used between page loads to avoid request bursts."""
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


# ---------- cookie import ----------

_SAME_SITE_MAP = {
    "lax": "Lax",
    "strict": "Strict",
    "none": "None",
    "no_restriction": "None",
    "unspecified": "Lax",
}


def _convert_cookie_list(cookies: list) -> list[dict]:
    """Convert a Cookie-Editor-style cookie array to Playwright cookie dicts."""
    out: list[dict] = []
    for c in cookies:
        if not isinstance(c, dict):
            continue
        name = c.get("name")
        domain = c.get("domain")
        if not name or not domain:
            continue
        same_site_raw = str(c.get("sameSite", "lax")).lower()
        expires = c.get("expirationDate", c.get("expires", -1))
        try:
            expires = float(expires)
        except (TypeError, ValueError):
            expires = -1
        out.append(
            {
                "name": name,
                "value": c.get("value", ""),
                "domain": domain,
                "path": c.get("path", "/"),
                "expires": expires,
                "httpOnly": bool(c.get("httpOnly", False)),
                "secure": bool(c.get("secure", False)),
                "sameSite": _SAME_SITE_MAP.get(same_site_raw, "Lax"),
            }
        )
    return out


def import_cookies_file(platform: str, source_path: Path) -> int:
    """Convert an exported cookie file into a platform's stored cookie set.

    Accepts either:
      - a Cookie-Editor style export (a JSON array of cookie objects), or
      - an already-Playwright storage_state object ({"cookies": [...], ...}).

    Writes ``.browser-profiles/<platform>-storage.json`` (loaded at browser
    startup via add_cookies) and returns the number of cookies.
    """
    raw = json.loads(source_path.read_text())
    if isinstance(raw, dict) and "cookies" in raw:
        storage = {"cookies": raw["cookies"], "origins": raw.get("origins", [])}
    elif isinstance(raw, list):
        storage = {"cookies": _convert_cookie_list(raw), "origins": []}
    else:
        raise ValueError(
            "Unrecognized cookie file format — expected a JSON array of cookies "
            "(Cookie-Editor export) or a Playwright storage_state object."
        )
    if not storage["cookies"]:
        raise ValueError("No usable cookies found in the file.")

    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    out = PROFILES_DIR / f"{platform}-storage.json"
    out.write_text(json.dumps(storage, indent=2))
    return len(storage["cookies"])
