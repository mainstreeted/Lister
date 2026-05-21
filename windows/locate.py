"""Find on-screen UI elements by description, using Claude's vision.

The applier never reads the DOM — that would be instrumentation, and
instrumentation is what we are avoiding. Instead it works the way a person
does: look at the screen, find the button, move the mouse there.

``Locator.find()`` takes a screenshot and a plain-English description
("the blue '1-Click Apply' button") and returns the pixel coordinates of
that element's center, or None if it is not visible.

Two backends:
  - "claude-cli": shells out to the local `claude` CLI (Ed's Claude Max
    subscription — no API key, no per-call cost). The default.
  - "api": the Anthropic SDK, if ANTHROPIC_API_KEY is set.
"""
from __future__ import annotations

import base64
import io
import json
import logging
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

_API_PROMPT = """This is a {w}x{h} pixel screenshot of a web browser.

Locate this element:
  {description}

Respond with ONLY one line of JSON and nothing else:
  {{"found": true, "x": <int>, "y": <int>}}   if you can see it
  {{"found": false}}                          if it is not visible

x and y are the pixel coordinates of the CENTER of the element, measured
from the top-left corner of the image."""


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
    def __init__(
        self,
        backend: str = "claude-cli",
        claude_cli_path: str = "claude",
        api_model: str = "claude-sonnet-4-6",
        timeout_s: int = 150,
    ):
        self.backend = backend
        self.claude_cli_path = claude_cli_path
        self.api_model = api_model
        self.timeout_s = timeout_s

    def find(self, image, description: str) -> tuple[int, int] | None:
        """Return (x, y) center of the described element, or None if absent."""
        w, h = image.size
        if self.backend == "api":
            result = self._find_api(image, description, w, h)
        else:
            result = self._find_cli(image, description, w, h)

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

    # ------------------------------------------------------------------

    def _find_cli(self, image, description: str, w: int, h: int) -> dict | None:
        with tempfile.TemporaryDirectory() as tmp:
            shot = Path(tmp) / "screen.png"
            image.save(shot)
            prompt = _PROMPT.format(
                path=str(shot), w=w, h=h, description=description
            )
            try:
                proc = subprocess.run(
                    [
                        self.claude_cli_path, "-p", prompt,
                        "--allowedTools", "Read",
                        "--output-format", "json",
                    ],
                    capture_output=True, text=True, timeout=self.timeout_s,
                )
            except FileNotFoundError:
                log.error(
                    "locate: `%s` not found. Install Claude Code on Windows "
                    "or set locator='api' in config.", self.claude_cli_path
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
            return _extract_json(text)

    def _find_api(self, image, description: str, w: int, h: int) -> dict | None:
        try:
            import anthropic
        except ImportError:
            log.error("locate: anthropic SDK not installed (pip install anthropic)")
            return None

        buf = io.BytesIO()
        image.save(buf, format="PNG")
        b64 = base64.standard_b64encode(buf.getvalue()).decode()

        try:
            client = anthropic.Anthropic()
            msg = client.messages.create(
                model=self.api_model,
                max_tokens=200,
                messages=[{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/png",
                                "data": b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": _API_PROMPT.format(
                                w=w, h=h, description=description
                            ),
                        },
                    ],
                }],
            )
        except Exception as e:
            log.warning("locate: Anthropic API call failed: %s", e)
            return None

        text = "".join(
            block.text for block in msg.content if block.type == "text"
        )
        return _extract_json(text)
