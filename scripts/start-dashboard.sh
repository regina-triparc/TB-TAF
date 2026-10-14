#!/usr/bin/env bash
# One command: set everything up, start the QA service and open the dashboard.
# macOS / Linux / Git Bash. On Windows PowerShell use scripts/start-dashboard.ps1.
#
# Same setup as run-seeded.sh (venv, requirements, .env, local tripbuilder-e2e, doctor),
# then starts the service and opens http://127.0.0.1:<port>/dashboard. "Full run" on a
# ticket runs triage -> test plan -> spec -> report; the spec is written into your local
# tripbuilder-e2e clone and run there. Nothing is committed or pushed unless --push.
# Stop with Ctrl+C.
#
# Usage: scripts/start-dashboard.sh [--source seed|jira|auto] [--provider claude|ollama|auto|offline]
#                                   [--test-repo ../tripbuilder-e2e] [--push] [--port 8000] [--no-browser]
set -euo pipefail

SOURCE=seed PROVIDER="" TEST_REPO="../tripbuilder-e2e" PUSH=0 PORT=8000 BROWSER=1
while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --test-repo) TEST_REPO="$2"; shift 2 ;;
    --push) PUSH=1; shift ;;
    --port) PORT="$2"; shift 2 ;;
    --no-browser) BROWSER=0; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
NEEDS_TEST_REPO=1
SCRIPT_NAME=scripts/start-dashboard.sh
# shellcheck source=lib/setup.sh
. "$(dirname "$0")/lib/setup.sh"

step "Starting the service"
if (exec 3<>"/dev/tcp/127.0.0.1/$PORT") 2>/dev/null; then
  fail "Port $PORT is already in use - probably a QA service started earlier, which still has its old settings. Stop it, or rerun with --port 8001."
fi
export TICKET_SOURCE="$SOURCE" PORT
URL="http://127.0.0.1:$PORT/dashboard"
echo "ticket source: $SOURCE   provider: ${PROVIDER:-from .env}"
if [ "$HAVE_REPO" = 1 ] && [ "$PUSH" = 0 ]; then echo "Full run writes specs to: $REPO/e2e/qa/"; fi
echo "dashboard:     $URL"
echo "stop with Ctrl+C"

if [ "$BROWSER" = 1 ]; then
  (
    for _ in $(seq 1 120); do
      if curl -fs "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        if command -v open >/dev/null; then open "$URL"
        elif command -v xdg-open >/dev/null; then xdg-open "$URL"
        elif command -v cmd.exe >/dev/null; then cmd.exe /c start "" "$URL"
        fi
        exit 0
      fi
      sleep 1
    done
  ) >/dev/null 2>&1 &
fi
exec "$VPY" -m taf serve --port "$PORT"
