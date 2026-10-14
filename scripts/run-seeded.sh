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

NEEDS_TEST_REPO=0
case "$STAGE" in automation|full) NEEDS_TEST_REPO=1 ;; esac
SCRIPT_NAME=scripts/run-seeded.sh
# shellcheck source=lib/setup.sh
. "$(dirname "$0")/lib/setup.sh"
if [ "$PUSH" = 0 ] && [ "$HAVE_REPO" = 0 ] && [ "$NEEDS_TEST_REPO" = 1 ]; then
  warn "stopping after the test plan (no local tripbuilder-e2e to write the spec into)"
  STAGE=architect
fi

step "Running $TICKET (seed) - stage $STAGE"
SPEC="$REPO/e2e/qa/$TICKET.spec.ts"
MARK="$(mktemp)"
set +e
if [ -n "$PROVIDER" ]; then "$VPY" -m taf run "$TICKET" --source seed --stage "$STAGE" --provider "$PROVIDER"
else "$VPY" -m taf run "$TICKET" --source seed --stage "$STAGE"; fi
CODE=$?
set -e

step "Result"
WRITTEN=0; [ -f "$SPEC" ] && [ "$SPEC" -nt "$MARK" ] && WRITTEN=1
rm -f "$MARK"
if [ "$PUSH" = 0 ] && [ "$HAVE_REPO" = 1 ] && [ "$WRITTEN" = 0 ]; then
  echo "no spec was written this run (offline provider, or the automation stage did not get that far - see above)"
fi
if [ "$PUSH" = 0 ] && [ "$HAVE_REPO" = 1 ] && [ "$WRITTEN" = 1 ]; then
  echo "spec written: $SPEC"
  echo "run it again:  (cd \"$REPO\" && npx playwright test e2e/qa/$TICKET.spec.ts --project=qa-agent --reporter=list)"
  echo "discard it:    (cd \"$REPO\" && git checkout -- e2e/qa/$TICKET.spec.ts)"
fi
if [ "$PUSH" = 1 ]; then echo "branch: https://github.com/regina-triparc/tripbuilder-e2e/tree/qa/$TICKET"; fi
echo "dashboard:     $VPY -m taf serve   ->  http://127.0.0.1:8000/dashboard"
exit $CODE
