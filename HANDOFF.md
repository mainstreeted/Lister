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
  `lister login`, `lister import-cookies`, `lister export-queue`,
  `lister import-results`.
- Config in `config/criteria.yaml` (gitignored); resumes in
  `config/resumes/*.md` (gitignored).
- **Apply — Windows macro applier** (`windows/`) — built this session; see
  "The apply step" below. Code-complete and verified on the WSL side; the
  Windows half is NOT yet tested on Ed's real machine.

## The apply step — Cloudflare, and how the macro applier solves it

ZipRecruiter / Indeed / LinkedIn are behind Cloudflare bot-detection. Every
attempt to drive a *launched* browser (Playwright, then patchright, then a
proposed Xvfb virtual display) hits Cloudflare's "Just a moment..." challenge.

**Key insight (Ed's, and it's correct):** the CAPTCHA/challenge is a *symptom
of a low trust score*, not a wall you defeat. Ed's everyday Chrome never gets
challenged because it has *earned trust* over months. Trust cannot be
manufactured — only inherited. Any fresh/launched browser starts at zero trust
and gets challenged. So the automation MUST run inside Ed's real, established,
everyday Chrome.

The `windows/` macro applier implements exactly that (see build status below).

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

## Windows macro applier — BUILT this session

Tool choice settled: **Python + pyautogui** (consistent with the codebase;
screenshots/JSON/Claude-vision are far easier than in AutoHotkey).

Lives in `windows/` — a standalone runtime, separate Python install, no shared
package with the WSL `lister` code. The two halves talk only via JSON files:

- WSL: `lister export-queue` → writes `data/apply-queue.json`. Marks exported
  jobs `queued` in the DB so they aren't re-exported.
- Windows: `python windows/applier.py` → reads the queue, focuses Ed's real
  Chrome, drives it with real OS mouse/keyboard input, writes
  `data/apply-results.json`.
- WSL: `lister import-results` → ingests outcomes into the DB.

`windows/` modules:
- `applier.py` — entry point: queue → per-job flow → results. Dry-run honored
  from the queue's flag (`--dry-run` can only make it *safer*, never force a
  real submit). Logs + debug screenshots under `windows/logs/`.
- `chrome.py` — focuses Chrome (pygetwindow), navigates via the omnibox
  (Ctrl+L + real typing), human-shaped mouse moves (eased, bowed path, slight
  overshoot), per-char typing jitter, randomized pauses.
- `locate.py` — finds on-screen elements by description via Claude vision.
  Backend `claude-cli` (default, free — Ed's Claude Max) or `api`. Never reads
  the DOM (that would be instrumentation).
- `applyqueue.py` — JSON queue/results I/O (named to avoid shadowing stdlib
  `queue`). Schema mirrors `src/lister/queue_io.py`.
- `config.example.json`, `requirements.txt`, `run_overnight.ps1` (Task
  Scheduler launcher), `README.md` (full setup).

ZR `/km/` expired-link handling: if no Apply button is found on the listing,
the applier falls back to ZipRecruiter search by `search_query`
(title + company), opens the best result, and looks again.

### NOT yet done — next session

1. **Test the Windows half on Ed's real machine.** None of `windows/` has run
   on Windows yet. First real test: `lister export-queue --limit 1` (dry-run),
   then `python windows/applier.py`, then check `windows/logs/` screenshots.
   Likely needs tuning: vision coordinate accuracy, the `claude-cli` invocation
   (`--allowedTools Read` permission prompt behavior on Windows), window focus.
2. Multi-step ZR forms — currently recorded as `skipped`. Add form-filling.
3. Indeed + LinkedIn flows in the applier (same macro approach).
4. Greenhouse + Indeed clean-API submitters (Greenhouse has no Cloudflare wall).
5. Tailoring layer (per-job resume variant + cover letter via Claude CLI).
6. `lister daily` orchestrator + email digest of what was submitted.

## How to run things (cold start)

In Ubuntu: `lg`  (alias for `cd ~/Lister && source .venv/bin/activate`)

- `lister discover --limit 25` — discover + rank, show table.
- `lister export-queue --limit 2` — write `data/apply-queue.json` (dry-run by
  default; `--no-dry-run` to authorize real submission).
- *(on Windows)* `python windows\applier.py` — drive real Chrome through the
  queue. See `windows/README.md` for setup.
- `lister import-results` — pull the Windows applier's outcomes into the DB.
- `lister status` — recent applications.
- `lister apply --limit 3` — the OLD launched-browser path; Cloudflare-blocked,
  superseded by the Windows applier. Kept for reference.

## How to resume in a fresh session

Tell the new agent: *"Continue the Lister build — read HANDOFF.md."*
The branch (`claude/resume-automation-tool-0hMKV`) has all committed code.
Local-only files (criteria.yaml, resumes, the DB, browser profiles) persist
on the WSL disk and are gitignored by design.
