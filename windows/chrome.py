"""Drive Ed's real, everyday Chrome with real OS input.

This is the whole point of the architecture: no launched browser, no
automation framework attached to the page. We send real Windows mouse and
keyboard events (``isTrusted: true``) to a Chrome window that already
exists and has earned its trust score. To a site, it is indistinguishable
from Ed using his computer.

Residual risk is *behavioral* — robotic cursor paths and timing. Every
function here is shaped to look human: eased multi-hop mouse moves with a
small overshoot, per-character typing jitter, randomized pauses.

Runs on Windows only. Requires: pyautogui, pygetwindow, pillow.
"""
from __future__ import annotations

import logging
import random
import time

import pyautogui
import pygetwindow as gw

log = logging.getLogger("applier.chrome")


def configure(failsafe: bool = True) -> None:
    """Set global pyautogui behavior. Call once at startup."""
    # FAILSAFE: slamming the mouse into a screen corner aborts the run.
    # Keep it on while Ed is testing; the overnight run never goes near a
    # corner, so it does not trip on its own.
    pyautogui.FAILSAFE = failsafe
    # We insert our own human-shaped pauses; kill the blanket one.
    pyautogui.PAUSE = 0.0


def pause(min_s: float, max_s: float) -> None:
    """Sleep a random human-ish interval."""
    time.sleep(random.uniform(min_s, max_s))


# ----------------------------------------------------------------------
# Window focus
# ----------------------------------------------------------------------

def focus_chrome() -> str:
    """Bring a Chrome window to the foreground. Returns the window title.

    Raises RuntimeError if no Chrome window is open — the applier requires
    Ed's real Chrome to already be running.
    """
    candidates = [
        w for w in gw.getAllWindows()
        if w.title and "chrome" in w.title.lower() and w.visible
    ]
    if not candidates:
        raise RuntimeError(
            "No Chrome window found. Open Ed's everyday Chrome before running "
            "the applier — this macro drives the real browser, it does not "
            "launch one."
        )
    # Prefer the largest window (the main browser, not a tiny popup).
    win = max(candidates, key=lambda w: w.width * w.height)
    try:
        if win.isMinimized:
            win.restore()
        win.maximize()
        win.activate()
    except Exception as e:  # window managers occasionally refuse activate()
        log.warning("focus_chrome: activate() complained: %s", e)
    pause(0.6, 1.2)
    return win.title


# ----------------------------------------------------------------------
# Keyboard
# ----------------------------------------------------------------------

def human_type(text: str) -> None:
    """Type a string with per-character delay jitter."""
    for ch in text:
        pyautogui.write(ch)
        time.sleep(random.uniform(0.04, 0.17))


def press(*keys: str) -> None:
    """Press a hotkey combo (e.g. press('ctrl', 'l'))."""
    pyautogui.hotkey(*keys)
    pause(0.2, 0.5)


def new_tab() -> None:
    press("ctrl", "t")
    pause(0.5, 1.0)


def close_tab() -> None:
    press("ctrl", "w")
    pause(0.4, 0.9)


def navigate(url: str) -> None:
    """Focus the address bar, type a URL, and go. Real keyboard, no scripting."""
    press("ctrl", "l")          # focus + select the omnibox
    pause(0.2, 0.5)
    human_type(url)
    pause(0.3, 0.7)
    pyautogui.press("enter")


# ----------------------------------------------------------------------
# Mouse
# ----------------------------------------------------------------------

def _hops(x0: float, y0: float, x1: float, y1: float) -> list[tuple[float, float]]:
    """Intermediate waypoints between two points, with perpendicular jitter."""
    dist = ((x1 - x0) ** 2 + (y1 - y0) ** 2) ** 0.5
    n = 1 if dist < 250 else 2
    pts: list[tuple[float, float]] = []
    for i in range(1, n + 1):
        t = i / (n + 1)
        # point on the straight line
        lx = x0 + (x1 - x0) * t
        ly = y0 + (y1 - y0) * t
        # nudge it sideways so the path bows like a human's
        jitter = min(40.0, dist * 0.08)
        pts.append((lx + random.uniform(-jitter, jitter),
                     ly + random.uniform(-jitter, jitter)))
    return pts


def human_move(x: int, y: int) -> None:
    """Move the cursor to (x, y) along an eased, slightly-bowed path.

    Ends with a tiny overshoot-and-correct, the way a real hand settles.
    """
    x0, y0 = pyautogui.position()
    for hx, hy in _hops(x0, y0, x, y):
        pyautogui.moveTo(
            round(hx), round(hy),
            duration=random.uniform(0.12, 0.30),
            tween=pyautogui.easeInOutQuad,
        )
    # small overshoot
    ox = x + random.randint(-6, 6)
    oy = y + random.randint(-6, 6)
    pyautogui.moveTo(ox, oy, duration=random.uniform(0.12, 0.25),
                     tween=pyautogui.easeOutQuad)
    # settle onto the target
    pyautogui.moveTo(x, y, duration=random.uniform(0.06, 0.14),
                     tween=pyautogui.easeInOutQuad)


def human_click(x: int, y: int) -> None:
    """Move to (x, y) like a human, pause, then click."""
    human_move(x, y)
    pause(0.15, 0.45)
    pyautogui.click()


def scroll(amount: int) -> None:
    """Scroll the page. Negative scrolls down."""
    pyautogui.scroll(amount)
    pause(0.4, 0.9)


def screenshot():
    """Grab a full-screen screenshot as a PIL Image."""
    return pyautogui.screenshot()
