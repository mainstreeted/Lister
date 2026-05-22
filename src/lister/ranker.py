"""Score jobs 0-100 against the user's criteria + resume.

Three rankers live here:

- ``MockRanker``: keyword-only, zero cost. Used for development.
- ``ClaudeCliRanker``: shells out to the local ``claude`` CLI in headless mode.
  Uses the user's Claude Code subscription (Pro/Max) for inference — no API key,
  no separate billing. This is the default for end users on a subscription plan.
- ``ApiRanker``: uses the Anthropic Python SDK with an ``ANTHROPIC_API_KEY``.
  Intended for users who specifically want to bill against API credits or run
  Lister on a machine without ``claude`` installed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess

from .config import Criteria
from .models import Job, MatchScore

log = logging.getLogger(__name__)

RANKER_MODEL = os.environ.get("LISTER_RANKER_MODEL", "claude-haiku-4-5-20251001")
RANKER_MAX_DESC_CHARS = 6000  # truncate very long JDs to keep tokens predictable
CLI_BATCH_SIZE = int(os.environ.get("LISTER_RANKER_BATCH", "15"))
CLI_TIMEOUT_SECONDS = int(os.environ.get("LISTER_RANKER_TIMEOUT", "600"))


SYSTEM_PROMPT_SINGLE = """\
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


SYSTEM_PROMPT_BATCH = """\
You are a hiring-fit evaluator helping a job seeker prioritize which postings to apply to.
You will receive the candidate's criteria + resume summary followed by a list of N job
postings, each identified by job_id.

Output a JSON array with EXACTLY N elements, in the same order as the input postings.
Each element must be an object with this exact shape — no prose, no markdown fences:

[
  {"job_id": "<exact id from input>", "score": <integer 0-100>, "reasons": [<3-6 short strings>], "dealbreakers": [<strings>]},
  ...
]

Scoring guide:
- 90-100: strong title+skill match, location works, no dealbreakers, level fits.
- 70-89: solid match with minor gaps.
- 50-69: plausible but stretchy — missing required skills or wrong level.
- <50: poor fit; don't apply.

Dealbreakers include: requires clearance candidate doesn't have, requires sponsorship
candidate can't provide, requires excluded skills, location mismatch with no remote
option, salary below floor (if stated), seniority far above/below targets.
"""


class MockRanker:
    """Keyword-only ranker. Zero cost, used for dev/CI."""

    def score(self, job: Job, criteria: Criteria, resume_summary: str) -> MatchScore:
        text = f"{job.title}\n{job.description_text}".lower()
        title_hits = sum(1 for t in criteria.targets.titles if t.lower() in text)
        skill_hits = sum(1 for s in criteria.targets.preferred_skills if s.lower() in text)
        raw = min(100, 30 + 10 * title_hits + 5 * skill_hits)
        return MatchScore(
            job_id=job.id,
            score=raw,
            reasons=[
                f"title keyword hits: {title_hits}",
                f"preferred skill hits: {skill_hits}",
                "mock-ranker (no LLM)",
            ],
        )

    def score_many(
        self, jobs: list[Job], criteria: Criteria, resume_summary: str
    ) -> list[MatchScore]:
        return [self.score(j, criteria, resume_summary) for j in jobs]


class ClaudeCliRanker:
    """Rank jobs by piping a batched prompt to the local ``claude`` CLI.

    Uses the user's Claude Code subscription. No ANTHROPIC_API_KEY needed.
    Batches jobs so the round-trip cost is fixed regardless of how many jobs
    were discovered.
    """

    def __init__(self, claude_binary: str | None = None, batch_size: int = CLI_BATCH_SIZE):
        self.claude = claude_binary or shutil.which("claude") or "claude"
        self.batch_size = batch_size

    def score(self, job: Job, criteria: Criteria, resume_summary: str) -> MatchScore:
        return self.score_many([job], criteria, resume_summary)[0]

    def score_many(
        self, jobs: list[Job], criteria: Criteria, resume_summary: str
    ) -> list[MatchScore]:
        results: list[MatchScore] = []
        for i in range(0, len(jobs), self.batch_size):
            batch = jobs[i : i + self.batch_size]
            try:
                results.extend(self._score_batch(batch, criteria, resume_summary))
            except Exception as e:
                log.warning("claude CLI ranker failed on batch (%d jobs): %s", len(batch), e)
                results.extend(
                    MatchScore(job_id=j.id, score=0, reasons=[f"ranker error: {e}"])
                    for j in batch
                )
        return results

    def _score_batch(
        self, batch: list[Job], criteria: Criteria, resume_summary: str
    ) -> list[MatchScore]:
        prompt = _build_batch_prompt(batch, criteria, resume_summary)
        cmd = [self.claude, "-p", prompt]
        log.info("claude CLI: scoring batch of %d jobs", len(batch))
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=CLI_TIMEOUT_SECONDS,
        )
        if proc.returncode != 0:
            raise RuntimeError(
                f"claude CLI exited {proc.returncode}: {proc.stderr.strip()[:500]}"
            )
        return _parse_batch_response(proc.stdout, batch)


class ApiRanker:
    """Calls the Anthropic SDK directly. Requires ANTHROPIC_API_KEY."""

    def __init__(self, client=None) -> None:
        # Import lazily so the rest of Lister works without `anthropic` installed.
        try:
            from anthropic import Anthropic
        except ImportError as e:
            raise RuntimeError(
                "ApiRanker requires the 'anthropic' package. "
                "Install with: pip install anthropic"
            ) from e
        self.client = client or Anthropic()

    def score(self, job: Job, criteria: Criteria, resume_summary: str) -> MatchScore:
        user_msg = _build_single_prompt(job, criteria, resume_summary)
        try:
            resp = self.client.messages.create(
                model=RANKER_MODEL,
                max_tokens=512,
                system=SYSTEM_PROMPT_SINGLE,
                messages=[{"role": "user", "content": user_msg}],
            )
            text = "".join(b.text for b in resp.content if b.type == "text")
            data = _extract_first_json_object(text)
            return MatchScore(
                job_id=job.id,
                score=int(data.get("score", 0)),
                reasons=list(data.get("reasons", [])),
                dealbreakers=list(data.get("dealbreakers", [])),
            )
        except Exception as e:
            log.warning("api ranker failed for %s: %s", job.id, e)
            return MatchScore(job_id=job.id, score=0, reasons=[f"ranker error: {e}"])

    def score_many(
        self, jobs: list[Job], criteria: Criteria, resume_summary: str
    ) -> list[MatchScore]:
        return [self.score(j, criteria, resume_summary) for j in jobs]


# -------- prompt construction --------


def _criteria_block(c: Criteria) -> str:
    blob = {
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
    out = f"CANDIDATE CRITERIA (JSON):\n{json.dumps(blob, indent=2)}\n"
    special = c.filters.special_constraints.strip()
    if special:
        out += f"\nSPECIAL CONSTRAINTS (these override defaults):\n{special}\n"
    return out


def _build_single_prompt(job: Job, c: Criteria, resume_summary: str) -> str:
    desc = job.description_text[:RANKER_MAX_DESC_CHARS]
    if len(job.description_text) > RANKER_MAX_DESC_CHARS:
        desc += "\n[...truncated]"
    return (
        f"{_criteria_block(c)}\n"
        f"CANDIDATE RESUME SUMMARY:\n{resume_summary}\n\n"
        f"JOB POSTING:\n"
        f"Company: {job.company}\n"
        f"Title: {job.title}\n"
        f"Location: {job.location} (remote={job.remote})\n"
        f"URL: {job.listing_url}\n\n"
        f"Description:\n{desc}\n"
    )


def _build_batch_prompt(jobs: list[Job], c: Criteria, resume_summary: str) -> str:
    job_blocks = []
    for j in jobs:
        desc = j.description_text[:RANKER_MAX_DESC_CHARS]
        if len(j.description_text) > RANKER_MAX_DESC_CHARS:
            desc += "\n[...truncated]"
        job_blocks.append(
            f"--- job_id: {j.id} ---\n"
            f"Company: {j.company}\n"
            f"Title: {j.title}\n"
            f"Location: {j.location} (remote={j.remote})\n"
            f"URL: {j.listing_url}\n"
            f"Description:\n{desc}\n"
        )
    return (
        f"{SYSTEM_PROMPT_BATCH}\n"
        f"{_criteria_block(c)}\n"
        f"CANDIDATE RESUME SUMMARY:\n{resume_summary}\n\n"
        f"JOBS ({len(jobs)} total — score every one, preserve order, echo job_id exactly):\n\n"
        + "\n".join(job_blocks)
        + f"\n\nRespond with a JSON array of exactly {len(jobs)} score objects."
    )


# -------- parsing --------


_JSON_OBJ_RE = re.compile(r"\{.*\}", re.DOTALL)
_JSON_ARR_RE = re.compile(r"\[.*\]", re.DOTALL)


def _extract_first_json_object(text: str) -> dict:
    m = _JSON_OBJ_RE.search(text)
    if not m:
        raise ValueError(f"no JSON object in model output: {text[:200]}")
    return json.loads(m.group(0))


def _extract_first_json_array(text: str) -> list:
    m = _JSON_ARR_RE.search(text)
    if not m:
        raise ValueError(f"no JSON array in model output: {text[:200]}")
    return json.loads(m.group(0))


def _parse_batch_response(text: str, batch: list[Job]) -> list[MatchScore]:
    """Parse the model's JSON array into MatchScore objects, matched by job_id.

    Resilient to extra prose around the array. If a job is missing from the
    response, we synthesize a score-0 placeholder so the caller doesn't lose
    track of it.
    """
    data = _extract_first_json_array(text)
    by_id: dict[str, dict] = {}
    for item in data:
        if isinstance(item, dict) and "job_id" in item:
            by_id[str(item["job_id"])] = item

    results = []
    for job in batch:
        item = by_id.get(job.id)
        if not item:
            results.append(
                MatchScore(
                    job_id=job.id,
                    score=0,
                    reasons=["batch response missing this job_id"],
                )
            )
            continue
        results.append(
            MatchScore(
                job_id=job.id,
                score=int(item.get("score", 0)),
                reasons=list(item.get("reasons", [])),
                dealbreakers=list(item.get("dealbreakers", [])),
            )
        )
    return results


def summarize_resume(resume_md: str, max_chars: int = 4000) -> str:
    if len(resume_md) <= max_chars:
        return resume_md
    return resume_md[:max_chars] + "\n[...truncated]"
