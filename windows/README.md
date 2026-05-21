# Lister — Windows Macro Applier

This is the **apply** half of Lister. The discovery + ranking "brain" runs in
WSL Ubuntu; this runs on **Windows**, because it has to drive Ed's *real,
everyday Chrome*.

## Why a separate Windows program

Sites like ZipRecruiter sit behind Cloudflare bot-detection. Every *launched*
browser — Playwright, patchright, a fresh Chrome on a virtual display — starts
with a zero trust score and gets the "Just a moment..." challenge. Trust is
earned over months of normal use; it can't be manufactured, only inherited.

So the applier doesn't launch anything. It sends **real Windows mouse and
keyboard events** (`isTrusted: true`) to the Chrome window Ed already uses
every day. To a website, it is indistinguishable from Ed at his desk. It's
meant to run **overnight (~4am)** via Task Scheduler, so "uninterrupted" is
solved by *time* — Ed is asleep — not by hiding the browser.

The residual risk is *behavioral* (robotic cursor paths / timing). The macro
mitigates that with eased, bowed mouse moves, per-character typing jitter,
randomized delays, and low volume (~1 application per platform per day).

## How the two halves talk

```
  WSL (brain)                         Windows (this applier)
  ───────────                         ──────────────────────
  lister discover   ──┐
  lister export-queue ─┼─► data/apply-queue.json  ──► applier.py
                       │                                  │ drives real Chrome
  lister import-results ◄── data/apply-results.json ◄──────┘
```

Plain JSON files, exchanged over the WSL↔Windows filesystem boundary. The WSL
repo is reachable from Windows at
`\\wsl.localhost\<distro>\home\<user>\Lister`.

## One-time setup

1. **Install Python 3.11+ for Windows** (python.org). Tick "Add to PATH".
2. **Install the dependencies** — in this `windows/` folder, in a Windows
   terminal (PowerShell / cmd, *not* WSL):
   ```
   pip install -r requirements.txt
   ```
3. **Element location backend.** The applier finds buttons by *looking* at
   the screen (it never touches the DOM). It uses Claude's vision:
   - `claude-cli` (default, free) — needs the **Claude Code CLI installed on
     Windows** and signed in to Ed's Claude Max plan. Verify with
     `claude --version` in a Windows terminal.
   - `api` — set `ANTHROPIC_API_KEY` and `"locator": "api"` in config. Costs a
     few cents per run.
4. **Create `config.json`** — copy `config.example.json` to `config.json` and
   edit the paths. The `_`-prefixed keys are comments; you can leave them.
   Get the exact WSL path by running `wslpath -w ~/Lister/data` in Ubuntu.

## Daily use

In **WSL**:
```
lister discover --limit 25            # find + rank jobs
lister export-queue --limit 2         # write apply-queue.json (dry-run by default)
```

On **Windows** — make sure Ed's normal Chrome is open and logged in to
ZipRecruiter, then:
```
python applier.py                     # honors the queue's dry-run flag
```

Back in **WSL**:
```
lister import-results                 # pull outcomes into the DB
lister status                         # review
```

`applier.py` flags:
- `--dry-run` — force a dry run even if the queue asked for real submission
  (it can only make things *safer*, never the reverse).
- `--limit N` — process at most N jobs.
- `--queue` / `--results` — override the paths from `config.json`.

A **dry run** navigates, finds the Apply button, and hovers over it without
clicking — proving the flow works. To actually submit, the WSL side must
export with `lister export-queue --no-dry-run`.

Every run writes a timestamped log and debug screenshots under `logs/` so Ed
can review exactly what the macro saw and did.

## Scheduling it overnight

`run_overnight.ps1` is a launcher for Windows Task Scheduler. Run it once by
hand to confirm it works, then register it (see the comment block at the top
of that file) to fire daily around 04:00. Note: Ed's Chrome must be running
and logged in for the scheduled run to succeed.

## Scope of v1

Handles **ZipRecruiter** 1-Click / Quick Apply. If a job leads to a
multi-step form or resume upload, the applier records `skipped` rather than
guessing. Indeed / LinkedIn / multi-step forms are future work.
