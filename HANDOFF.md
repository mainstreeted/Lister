# Lister — Session Handoff

Read this first when resuming. It captures project state, the architecture
decision, and what's next, so a fresh session is oriented in one read.

## What Lister is

Personal job-application automation for Ed Murphy (Tallahassee, FL — insurance /
customer-service / operations background, P&C licensed). Pipeline:
**discover jobs → rank them → apply to them.** Runs from WSL Ubuntu on a
Windows machine. Branch: `claude/resume-automation-tool-0hMKV` (PR #1).

## What WORKS (do not rebuild)

- **Discovery**
  - Greenhouse connector — public board API, ~10 working company slugs.
  - LinkedIn connector — exists, but LinkedIn flagged the account; treat as
    digest-only for now.
  - **Email-digest connector** — reads job-alert emails from a burner Gmail
    (`claudialinkage@gmail.com`) via IMAP. Ed forwards/redirects Indeed +
    ZipRecruiter alerts there. Parses Indeed CTS + ZipRecruiter /km/ tracker
    URLs, subject-line titles, etc. This is the primary discovery source.
- **Ranking** — `ClaudeCliRanker` shells out to the local `claude` CLI
  (uses Ed's Claude Max subscription — no API key, no extra cost). Scores
  jobs 0-100 against criteria + resume.
- **Storage** — SQLite (`data/lister.sqlite`): jobs, scores, applications.
- **CLI** — `lister discover`, `lister apply`, `lister status`,
  `lister login`, `lister import-cookies`.
- Config in `config/criteria.yaml` (gitignored); resumes in
  `config/resumes/*.md` (gitignored).

## The OPEN PROBLEM — the `apply` step

ZipRecruiter / Indeed / LinkedIn are behind Cloudflare bot-detection. Every
attempt to drive a *launched* browser (Playwright, then patchright, then a
proposed Xvfb virtual display) hits Cloudflare's "Just a moment..." challenge.

**Key insight (Ed's, and it's correct):** the CAPTCHA/challenge is a *symptom
of a low trust score*, not a wall you defeat. Ed's everyday Chrome never gets
challenged because it has *earned trust* over months. Trust cannot be
manufactured — only inherited. Any fresh/launched browser starts at zero trust
and gets challenged. So the automation MUST run inside Ed's real, established,
everyday Chrome.

## ARCHITECTURE DECISION (locked this session)

**Apply via an OS-level input macro driving Ed's real everyday Chrome,
scheduled to run overnight (~4am).**

Why this and nothing else:
- Real everyday Chrome → trust already earned → no CAPTCHA. (The requirement.)
- Real OS mouse/keyboard events (`isTrusted: true`) → zero automation
  fingerprint, nothing attached to the browser. (Ed: "backdoor is backdoor.")
- Scheduled overnight → "background"/uninterrupted solved by *time*, not by a
  virtual display. Ed is asleep; macro runs 1-2 min; done.

Rejected and why:
- Playwright / patchright / Xvfb — all launch a *stranger* browser, zero trust,
  get Cloudflare-challenged.
- Browser extension — runs in the real browser but dispatches synthetic
  (`isTrusted: false`) events; still detectable, less clean than OS input.
- CDP-attach — works but Ed rejected (and it's still instrumentation).
- Paid CAPTCHA solver — Ed has no budget.

Residual risk (accepted): behavioral detection (robotic mouse paths/timing).
Mitigate with human-shaped cursor curves + randomized delays + low volume
(~1 application/platform/day). Worst case: make a new account.

## NEXT SESSION — build plan

1. Build a **Windows-side macro applier** (Ed's real Chrome lives on Windows;
   the macro must run on Windows). Tool choice to settle: AutoHotkey vs.
   Python-on-Windows with `pyautogui` — both do real OS input.
2. Flow: read the apply-queue → for each job, drive real Chrome with real
   input (open URL, locate Apply button via screenshot, click, fill, submit) →
   write results back.
3. Element location: screenshot the screen, locate the target (image match or
   hand the screenshot to Claude). Not blind coordinate replay.
4. The WSL brain (discover/rank/DB) is unchanged; it writes the apply-queue to
   a file the Windows macro reads across the WSL/Windows boundary.
5. ZipRecruiter `/km/` email links expire — the macro should search ZR by job
   title + company to reach a live posting, not trust the dead tracking link.
6. Schedule it (Windows Task Scheduler) for ~4am.

Also still open / lower priority:
- Greenhouse + Indeed application submitters (clean-API path; Greenhouse has
  no Cloudflare wall — it is the most automatable platform if ever needed).
- Tailoring layer (per-job resume variant + cover letter via Claude CLI).
- `lister daily` orchestrator + email digest of what was submitted.

## How to run things (cold start)

In Ubuntu: `lg`  (alias for `cd ~/Lister && source .venv/bin/activate`)

- `lister discover --limit 25` — discover + rank, show table.
- `lister apply --limit 3` — dry-run apply (currently blocked by Cloudflare —
  see open problem).
- `lister status` — recent applications.

## How to resume in a fresh session

Tell the new agent: *"Continue the Lister build — read HANDOFF.md."*
The branch (`claude/resume-automation-tool-0hMKV`) has all committed code.
Local-only files (criteria.yaml, resumes, the DB, browser profiles) persist
on the WSL disk and are gitignored by design.
