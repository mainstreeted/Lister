#!/usr/bin/env bash
# Lister installer. Clones the repo, sets up a Python virtualenv, installs deps.
#
# Usage (from any machine with git + python3.11+ + curl):
#   curl -fsSL https://raw.githubusercontent.com/mainstreeted/Lister/claude/resume-automation-tool-0hMKV/scripts/setup.sh | bash
#
# Optional env vars:
#   LISTER_HOME   target directory (default: ~/Lister)
#   LISTER_BRANCH branch to check out (default: claude/resume-automation-tool-0hMKV)

set -euo pipefail

BRANCH="${LISTER_BRANCH:-claude/resume-automation-tool-0hMKV}"
TARGET="${LISTER_HOME:-$HOME/Lister}"
REPO_URL="https://github.com/mainstreeted/Lister.git"

say() { printf "\033[1;36m[lister-setup]\033[0m %s\n" "$*"; }
die() { printf "\033[1;31m[lister-setup]\033[0m %s\n" "$*" >&2; exit 1; }

command -v git >/dev/null      || die "git not found. Install Git and re-run."
command -v curl >/dev/null     || die "curl not found. Install curl and re-run."

# Find a Python >= 3.11
PY=""
for cand in python3.13 python3.12 python3.11 python3; do
  if command -v "$cand" >/dev/null; then
    if "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)' 2>/dev/null; then
      PY="$cand"; break
    fi
  fi
done
[[ -n "$PY" ]] || die "Python 3.11 or newer is required. On macOS: 'brew install python@3.11'. On Ubuntu: 'sudo apt install python3.11 python3.11-venv'."

say "Using Python: $($PY --version)"

# Clone or refresh
if [[ -d "$TARGET/.git" ]]; then
  say "Updating existing checkout at $TARGET"
  git -C "$TARGET" fetch origin --quiet
  git -C "$TARGET" checkout "$BRANCH" --quiet
  git -C "$TARGET" pull --ff-only origin "$BRANCH" --quiet
elif [[ -e "$TARGET" ]]; then
  die "$TARGET exists but isn't a git checkout. Move or delete it and re-run."
else
  say "Cloning $REPO_URL -> $TARGET"
  git clone --quiet "$REPO_URL" "$TARGET"
  git -C "$TARGET" checkout --quiet "$BRANCH"
fi

cd "$TARGET"

# venv
if [[ ! -d .venv ]]; then
  say "Creating virtualenv at $TARGET/.venv"
  if ! "$PY" -m venv .venv 2>/dev/null; then
    # Common on fresh Ubuntu/Debian: ensurepip / venv module not packaged.
    pyver=$("$PY" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
    die "Failed to create virtualenv. On Ubuntu/Debian run:

  sudo apt update && sudo apt install -y python${pyver}-venv

Then re-run this installer."
  fi
fi
# shellcheck disable=SC1091
source .venv/bin/activate

say "Installing dependencies (this can take ~30s the first time)"
pip install --quiet --upgrade pip
pip install --quiet -e ".[dev]"

# Make sure config dirs exist (they're gitignored)
mkdir -p config/resumes data

# Playwright browser binary (~170MB; idempotent — fast if already present).
say "Installing Playwright Chromium browser (~170MB; one-time)"
if ! python -m playwright install chromium >/dev/null 2>&1; then
    say "WARNING: 'playwright install chromium' failed."
    say "If browser-based discovery (LinkedIn etc.) errors out, run manually:"
    say "  source .venv/bin/activate && playwright install chromium"
fi

cat <<NEXT

\033[1;32m✓ Lister is installed at:\033[0m $TARGET

To use it:
  cd $TARGET
  source .venv/bin/activate
  lister --help

If you don't yet have a config/criteria.yaml or resumes/*.md, run:
  cp config/criteria.example.yaml config/criteria.yaml
  # then edit config/criteria.yaml and drop resume markdown files into config/resumes/

To smoke-test discovery with no API key (mock ranker):
  lister discover --mock-ranker --limit 10

NEXT
