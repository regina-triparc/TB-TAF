#!/usr/bin/env bash
# One command from a fresh clone to a generated Playwright test for the seeded ticket.
# macOS / Linux / Git Bash. On Windows PowerShell use scripts/run-seeded.ps1.
#
#   1. finds Python 3.11+, creates .venv, installs requirements (only when they changed)
#   2. checks .env (the one you were sent; falls back to .env.example)
#   3. runs the seeded demo ticket (TB-601, TICKET_SOURCE=seed) through the pipeline
#   4. writes the spec into YOUR local tripbuilder-e2e clone (e2e/qa/<TICKET>.spec.ts)
#      and runs it - nothing is committed or pushed unless you pass --push
#
# Usage: scripts/run-seeded.sh [--ticket TB-601] [--stage full] [--provider claude|ollama|auto|offline]
#                              [--test-repo ../tripbuilder-e2e] [--push]
set -euo pipefail

TICKET=TB-601 STAGE=full PROVIDER="" TEST_REPO="../tripbuilder-e2e" PUSH=0
while [ $# -gt 0 ]; do
  case "$1" in
    --ticket) TICKET="$2"; shift 2 ;;
    --stage) STAGE="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --test-repo) TEST_REPO="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    -h|--help) sed -n '2,14p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
step() { printf '\n== %s\n' "$1"; }
fail() { printf '\n[FAIL] %s\n' "$1" >&2; exit 1; }

# ---- 1. Python + virtualenv -------------------------------------------------
step "Python environment"
if [ -x .venv/bin/python ]; then VPY=.venv/bin/python
elif [ -x .venv/Scripts/python.exe ]; then VPY=.venv/Scripts/python.exe
else
  BASE=""
  for c in python3.12 python3.11 python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)'; then
      BASE="$c"; break
    fi
  done
  [ -n "$BASE" ] || fail "Python 3.11+ not found."
  echo "creating .venv with $BASE"
  "$BASE" -m venv .venv
  if [ -x .venv/bin/python ]; then VPY=.venv/bin/python; else VPY=.venv/Scripts/python.exe; fi
fi
HASH="$("$VPY" -c 'import hashlib; print(hashlib.sha256(open("requirements.txt","rb").read()).hexdigest())')"
if [ "$(cat .venv/.requirements.sha256 2>/dev/null || true)" != "$HASH" ]; then
  echo "installing requirements (first run takes a few minutes)"
  "$VPY" -m pip install --quiet --disable-pip-version-check -r requirements.txt || fail "pip install failed"
  echo "$HASH" > .venv/.requirements.sha256
else
  echo "requirements already installed"
fi

# ---- 2. .env ----------------------------------------------------------------
step ".env"
if [ ! -f .env ]; then
  cp .env.example .env
  echo "[warn] no .env - copied .env.example. Put the values you were sent into .env for a real model."
else
  echo ".env present"
fi

# ---- 3. Where the generated test goes ---------------------------------------
step "Test repository"
case "$TEST_REPO" in /*|[A-Za-z]:*) REPO="$TEST_REPO" ;; *) REPO="$ROOT/$TEST_REPO" ;; esac
HAVE_REPO=0; [ -f "$REPO/playwright.config.ts" ] && HAVE_REPO=1
if [ "$PUSH" = 1 ]; then
  echo "managed mode from .env: the agent's own clone, branch qa/$TICKET is committed and pushed"
elif [ "$HAVE_REPO" = 1 ]; then
  REPO="$(cd "$REPO" && pwd)"
  echo "local mode: spec is written to $REPO/e2e/qa/$TICKET.spec.ts (not committed, not pushed)"
  export TEST_REPO_LOCAL_PATH="$REPO" TEST_REPO_NO_GIT=true GIT_PUSH_ENABLED=false
  if [ -f "$REPO/.env" ]; then export TEST_REPO_ENV_FILE="$REPO/.env"
  else echo "[warn] $REPO/.env missing - add TBA_E2E_EMAIL / TBA_E2E_PASSWORD there or the spec cannot sign in"; fi
  if [ "$STAGE" = automation ] || [ "$STAGE" = full ]; then
    (cd "$REPO" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npx playwright install chromium >/dev/null)
  fi
else
  echo "[warn] no tripbuilder-e2e clone at $REPO - stopping after the test plan."
  echo "       To get tests generated: git clone https://github.com/regina-triparc/tripbuilder-e2e.git \"$REPO\""
  if [ "$STAGE" = automation ] || [ "$STAGE" = full ]; then STAGE=architect; fi
fi

# ---- 4. Check, then run -----------------------------------------------------
step "Setup check"
if ! "$VPY" -m taf doctor && [ "$PROVIDER" != offline ]; then
  fail "setup check failed (see above). Fix it, or prove the pipeline without a model: scripts/run-seeded.sh --provider offline"
fi

step "Running $TICKET (seed) - stage $STAGE"
set +e
if [ -n "$PROVIDER" ]; then "$VPY" -m taf run "$TICKET" --source seed --stage "$STAGE" --provider "$PROVIDER"
else "$VPY" -m taf run "$TICKET" --source seed --stage "$STAGE"; fi
CODE=$?
set -e

step "Result"
if [ "$PUSH" = 0 ] && [ "$HAVE_REPO" = 1 ] && [ -f "$REPO/e2e/qa/$TICKET.spec.ts" ]; then
  echo "spec: $REPO/e2e/qa/$TICKET.spec.ts"
  echo "run it again:  (cd \"$REPO\" && npx playwright test e2e/qa/$TICKET.spec.ts --project=qa-agent --reporter=list)"
  echo "discard it:    (cd \"$REPO\" && git checkout -- e2e/qa/$TICKET.spec.ts)"
fi
if [ "$PUSH" = 1 ]; then echo "branch: https://github.com/regina-triparc/tripbuilder-e2e/tree/qa/$TICKET"; fi
echo "dashboard:     $VPY -m taf serve   ->  http://127.0.0.1:8000/dashboard"
exit $CODE
