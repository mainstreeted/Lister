"""Score a job 0-100 against the user's criteria + resume.

Uses Claude with a structured JSON output. Cheap model (Haiku) by default since
ranking runs against every discovered job; tailoring later uses Sonnet/Opus.
"""
from __future__ import annotations

import json
import logging
import os
import re

from anthropic import Anthropic

from .config import Criteria
from .models import Job, MatchScore

log = logging.getLogger(__name__)

RANKER_MODEL = os.environ.get("LISTER_RANKER_MODEL", "claude-haiku-4-5-20251001")
RANKER_MAX_DESC_CHARS = 6000  # truncate very long JDs to keep tokens predictable


SYSTEM_PROMPT = """\
You are a hiring-fit evaluator helping a job seeker prioritize which postings to apply to.
You will receive (a) the candidate's criteria + resume summary, and (b) one job posting.
Output a single JSON object — no prose, no markdown fences — with this exact shape:

{
  "score": <integer 0-100>,
  "reasons": [<3-6 short strings explaining the score>],
  "dealbreakers": [<strings; empty array if none>]
}

Scoring guide:
- 90-100: strong title+skill match, location works, no dealbreakers, level fits.
- 70-89: solid match with minor gaps.
- 50-69: plausible but stretchy — missing required skills or wrong level.
- <50: poor fit; don't apply.

Dealbreakers include: requires clearance candidate doesn't have, requires
sponsorship candidate can't provide, requires excluded skills, location mismatch
with no remote option, salary below floor (if stated), seniority far above/below targets.
"""


class Ranker:
    def __init__(self, client: Anthropic | None = None) -> None:
        self.client = client or Anthropic()

    def score(self, job: Job, criteria: Criteria, resume_summary: str) -> MatchScore:
        user_msg = _build_user_message(job, criteria, resume_summary)
        try:
            resp = self.client.messages.create(
                model=RANKER_MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            data = _extract_json(text)
            return MatchScore(
                job_id=job.id,
                score=int(data.get("score", 0)),
                reasons=list(data.get("reasons", [])),
                dealbreakers=list(data.get("dealbreakers", [])),
            )
        except Exception as e:
            log.warning("ranker failed for %s: %s", job.id, e)
            return MatchScore(job_id=job.id, score=0, reasons=[f"ranker error: {e}"])


def _build_user_message(job: Job, c: Criteria, resume_summary: str) -> str:
    desc = job.description_text[:RANKER_MAX_DESC_CHARS]
    if len(job.description_text) > RANKER_MAX_DESC_CHARS:
        desc += "\n[...truncated]"

    criteria_blob = {
        "target_titles": c.targets.titles,
        "preferred_skills": c.targets.preferred_skills,
        "exclude_required_skills": c.targets.exclude_required_skills,
        "levels": c.targets.levels,
        "locations": c.filters.locations,
        "remote_ok": c.filters.remote_ok,
        "min_salary_usd": c.filters.min_salary_usd,
        "needs_sponsorship": c.filters.needs_sponsorship,
        "has_security_clearance": c.filters.has_security_clearance,
    }
    return (
        "CANDIDATE CRITERIA (JSON):\n"
        f"{json.dumps(criteria_blob, indent=2)}\n\n"
        "CANDIDATE RESUME SUMMARY:\n"
        f"{resume_summary}\n\n"
        "JOB POSTING:\n"
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location} (remote={job.remote})\n"
        f"URL: {job.listing_url}\n\n"
        f"Description:\n{desc}\n"
    )


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def _extract_json(text: str) -> dict:
    """Find the first JSON object in the model's output, tolerating any stray prose."""
    m = _JSON_RE.search(text)
    if not m:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(m.group(0))


def summarize_resume(resume_md: str, max_chars: int = 4000) -> str:
    """Trim resume markdown to a budget — passed into every ranker call as context."""
    if len(resume_md) <= max_chars:
        return resume_md
    return resume_md[:max_chars] + "\n[...truncated]"
