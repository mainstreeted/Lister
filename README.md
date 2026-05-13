# Lister

Personal job-application automation. Discovers postings on configured platforms,
ranks them against your criteria and resume, tailors a resume+cover-letter per
job, and (eventually) submits applications on your behalf.

**Status:** scaffolding + Greenhouse discovery + Claude-based ranker. Submission
layer not yet wired.

## What's built

- `lister discover` — pulls jobs from every enabled platform, scores them with
  Claude, writes everything to a local SQLite DB, and prints a ranked table.
- Greenhouse connector (public board JSON API — no auth, no scraping).
- Pluggable connector interface for Lever, Ashby, Workday, LinkedIn, Indeed,
  ZipRecruiter (stubs to be added).
- Config: `config/criteria.yaml` (filters, targets, platforms, daily caps).
- Resumes: `config/resumes/granular.md` + `formal.md` (gitignored, your private
  source of truth for the tailorer).

## Setup

```bash
# 1. Install
python3.11 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure secrets
cp .env.example .env
# fill in ANTHROPIC_API_KEY (required), plus site creds when those connectors land

# 3. Configure criteria
cp config/criteria.example.yaml config/criteria.yaml
# edit profile, targets, filters, and enable the Greenhouse companies you want scanned

# 4. Drop resumes
# create config/resumes/granular.md and config/resumes/formal.md
# (see config/resumes/README.md for format)

# 5. Run
lister discover
lister status
```

## Layout

```
src/lister/
├── cli.py             # typer entry point
├── config.py          # pydantic models for criteria.yaml
├── models.py          # Job, MatchScore, Application
├── db.py              # sqlite state
├── discovery/         # one connector per platform
│   ├── base.py
│   └── greenhouse.py
└── ranker.py          # Claude-based 0-100 fit score
```

## Roadmap

- [ ] Lever, Ashby, Workday connectors (similar shape to Greenhouse).
- [ ] LinkedIn / Indeed / ZipRecruiter via Playwright with persistent profile.
- [ ] `tailor.py` — generate per-job resume variant + cover letter, render to PDF.
- [ ] `apply/` — per-platform submitters that drive the form fill.
- [ ] `lister daily` — orchestrator: discover → rank → tailor → submit with caps.
- [ ] Email digest of what got submitted.

## Operating principles

- One application per platform per day, max 5/day total. This is *not* mass-apply;
  it's a highly tuned digital replica of you applying to roles you'd actually take.
- Persistent logged-in browser profiles per platform — your session looks like a
  human's because it is one (you logged in once).
- Dry-run by default. Submission only flips on after you've reviewed a week of
  shortlists.
- Resumes and credentials never leave your machine except as inputs to the
  Anthropic API at tailoring time.

## Testing

```bash
pytest
```
