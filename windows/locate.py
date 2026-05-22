"""Find on-screen UI elements by description, using Claude's vision.

The applier never reads the DOM — that would be instrumentation, and
instrumentation is what we are avoiding. Instead it works the way a person
does: look at the screen, find the button, move the mouse there.

``Locator.find()`` takes a screenshot and a plain-English description
("the blue '1-Click Apply' button") and returns the pixel coordinates of
that element's center, or None if it is not visible.

It does this by handing the screenshot to the local ``claude`` CLI — Ed's
Claude Max subscription, no API key, no per-call cost. This whole step
happens off to the side, between this machine and Claude; it never touches
the browser, so the job site cannot see it.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger("applier.locate")

_PROMPT = """You are a precise UI element locator.

An image is saved at this path: {path}
It is a {w}x{h} pixel screenshot of a web browser.

Use the Read tool to view that image, then locate this element:
  {description}

Respond with ONLY one line of JSON and nothing else:
  {{"found": true, "x": <int>, "y": <int>}}   if you can see it
  {{"found": false}}                          if it is not visible

x and y are the pixel coordinates of the CENTER of the element, measured
from the top-left corner of the image."""


# Screenshots are resized to this width before being shown to the model.
# Full-resolution shots get downscaled unpredictably by the vision pipeline,
# which ruins coordinate accuracy; a fixed modest size keeps it consistent.
_MODEL_WIDTH = 1280


def _extract_json(text: str) -> dict | None:
    """Pull the last {...} JSON object out of a blob of model output."""
    matches = re.findall(r"\{[^{}]*\}", text)
    for blob in reversed(matches):
        try:
            return json.loads(blob)
        except json.JSONDecodeError:
            continue
    return None


class Locator:
    def __init__(self, claude_cli_path: str = "claude", timeout_s: int = 150):
        self.claude_cli_path = claude_cli_path
        self.timeout_s = timeout_s

    def find(self, image, description: str) -> tuple[int, int] | None:
        """Return (x, y) center of the described element, or None if absent."""
        w, h = image.size
        result = self._ask_claude(image, description, w, h)

        if not result or not result.get("found"):
            log.info("locate: %r — not found", description)
            return None
        try:
            x, y = int(result["x"]), int(result["y"])
        except (KeyError, ValueError, TypeError):
            log.warning("locate: %r — malformed coords %r", description, result)
            return None
        # Clamp to the image so a hallucinated coordinate can't click off-screen.
        x = max(0, min(w - 1, x))
        y = max(0, min(h - 1, y))
        log.info("locate: %r — found at (%d, %d)", description, x, y)
        return x, y

    def _ask_claude(self, image, description: str, w: int, h: int) -> dict | None:
        # Resize to a fixed width so the model always works in a known,
        # consistent coordinate space; scale its answer back up afterwards.
        if w > _MODEL_WIDTH:
            ratio = _MODEL_WIDTH / w
            small = image.resize((_MODEL_WIDTH, max(1, round(h * ratio))))
        else:
            ratio = 1.0
            small = image
        sw, sh = small.size

        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "screen.png"
            small.save(shot)
            prompt = _PROMPT.format(
                path=str(shot), w=sw, h=sh, description=description
            )
            # The prompt goes in on stdin — `claude -p` reads it there when no
            # positional prompt is given. It is multi-line; keeping it off the
            # command line avoids every shell-quoting pitfall.
            cmd = [
                self.claude_cli_path, "-p",
                "--allowedTools", "Read",
                "--output-format", "json",
            ]
            # Windows: npm installs Claude Code as `claude.cmd`. CreateProcess
            # cannot launch a .cmd/.bat directly, so route it through cmd.exe.
            if os.name == "nt" and self.claude_cli_path.lower().endswith(
                (".cmd", ".bat")
            ):
                cmd = ["cmd", "/c", *cmd]
            try:
                proc = subprocess.run(
                    cmd, input=prompt,
                    capture_output=True, text=True, timeout=self.timeout_s,
                )
            except FileNotFoundError:
                log.error(
                    "locate: `%s` not found. Install Claude Code on Windows "
                    "and set its path in config.json (claude_cli_path).",
                    self.claude_cli_path,
                )
                return None
            except subprocess.TimeoutExpired:
                log.warning("locate: claude CLI timed out after %ds", self.timeout_s)
                return None

            if proc.returncode != 0:
                log.warning("locate: claude CLI exit %d: %s",
                            proc.returncode, proc.stderr[:300])
                return None

            # `--output-format json` wraps the reply; `.result` holds the text.
            text = proc.stdout
            try:
                outer = json.loads(proc.stdout)
                text = outer.get("result", proc.stdout)
            except json.JSONDecodeError:
                pass
            result = _extract_json(text)

        # Scale coordinates from the resized image back to full resolution.
        if result and result.get("found") and ratio != 1.0:
            for key in ("x", "y"):
                val = result.get(key)
                if isinstance(val, (int, float)):
                    result[key] = round(val / ratio)
        return result
