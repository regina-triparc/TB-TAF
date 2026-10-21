#!/usr/bin/env bash
# Run the same eval case(s) on two or more providers and open a visual comparison.
#
#   scripts/compare-models.sh                                  # tb-601 on ollama, then claude
#   scripts/compare-models.sh --case tb-601 --case sample-password-reset --with-repo
#   scripts/compare-models.sh --providers offline,ollama       # free
#   scripts/compare-models.sh --langfuse                       # also trace into Langfuse ("eval")
#
# Nothing is committed, pushed or opened as a PR, and no run history is written.
# Claude costs money (TB-601: about $0.70-0.90 per run); Ollama takes minutes on CPU.
# Results go to evals/results/ (git-ignored).
set -euo pipefail
cd "$(dirname "$0")/.."

PROVIDERS="ollama,claude"; STAGE=automation; CASES=(); EXTRA=(); OPEN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --providers) PROVIDERS="$2"; shift 2 ;;
    --case) CASES+=("$2"); shift 2 ;;
    --stage) STAGE="$2"; shift 2 ;;
    --with-repo) EXTRA+=(--with-repo); shift ;;
    --judge) EXTRA+=(--judge); shift ;;
    --langfuse) EXTRA+=(--langfuse); shift ;;
    --no-browser) OPEN=0; shift ;;
    -h|--help) sed -n '2,11p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
[ ${#CASES[@]} -eq 0 ] && CASES=(tb-601)

VPY=.venv/bin/python; [ -x "$VPY" ] || VPY=.venv/Scripts/python.exe
[ -x "$VPY" ] || { echo "[FAIL] no .venv yet - run scripts/run-seeded.sh once, then rerun." >&2; exit 1; }
[ -f .env ] || { echo "[FAIL] no .env - copy .env.example to .env and fill it in." >&2; exit 1; }

export GIT_PUSH_ENABLED=false PR_CREATE_ENABLED=false TICKET_SOURCE=seed
STAMP="$(date +%Y%m%d-%H%M%S)"
RESULTS=(); N=0
IFS=',' read -r -a PLIST <<< "$PROVIDERS"
[ ${#PLIST[@]} -ge 2 ] || { echo "[FAIL] give at least two providers, e.g. --providers ollama,claude" >&2; exit 1; }
for P in "${PLIST[@]}"; do
  echo; echo "== Eval on $P (${CASES[*]} through $STAGE)"
  N=$((N + 1)); OUT="evals/results/compare-$STAMP-$N-$P.json"
  ARGS=(-m taf eval --provider "$P" --stage "$STAGE" --label "$P" --out "$OUT")
  for C in "${CASES[@]}"; do ARGS+=(--case "$C"); done
  "$VPY" "${ARGS[@]}" "${EXTRA[@]}" || true   # exit 1 = a case failed its checks: still worth comparing
  [ -f "$OUT" ] || { echo "[FAIL] the $P eval produced no result file" >&2; exit 1; }
  RESULTS+=("$OUT")
done

echo; echo "== Report"
REPORT="evals/results/compare-$STAMP.html"
"$VPY" -m taf eval report "${RESULTS[@]}" --out "$REPORT"
"$VPY" -m taf eval compare "${RESULTS[0]}" "${RESULTS[-1]}"
echo; echo "report: $(pwd)/$REPORT"
if [ "$OPEN" = 1 ]; then
  if command -v open >/dev/null; then open "$REPORT"; elif command -v xdg-open >/dev/null; then xdg-open "$REPORT"; fi
fi
