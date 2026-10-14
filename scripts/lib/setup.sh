# Shared setup for scripts/run-seeded.sh and scripts/start-dashboard.sh. Source it.
#
# Expects from the caller: PROVIDER, TEST_REPO, PUSH (0/1), NEEDS_TEST_REPO (0/1),
# SCRIPT_NAME. Sets: ROOT, VPY, REPO, HAVE_REPO, and exports the env overrides that
# make generated specs go to the local tripbuilder-e2e clone without committing or pushing.

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
step() { printf '\n== %s\n' "$1"; }
warn() { printf '[warn] %s\n' "$1"; }
fail() { printf '\n[FAIL] %s\n' "$1" >&2; exit 1; }

# ---- Python + virtualenv ----------------------------------------------------
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

# ---- .env ---------------------------------------------------------------------
step ".env"
if [ ! -f .env ]; then
  cp .env.example .env
  warn "no .env - copied .env.example. Put the values you were sent into .env for a real model."
else
  echo ".env present"
fi
if [ -n "$PROVIDER" ]; then export LLM_PROVIDER="$PROVIDER"; fi

# ---- Where generated tests go -----------------------------------------------
step "Test repository"
case "$TEST_REPO" in /*|[A-Za-z]:*) REPO="$TEST_REPO" ;; *) REPO="$ROOT/$TEST_REPO" ;; esac
HAVE_REPO=0; [ -f "$REPO/playwright.config.ts" ] && HAVE_REPO=1
if [ "$PUSH" = 1 ]; then
  echo "push mode (.env settings): the agent's own clone; qa/<TICKET> branches are committed and pushed"
elif [ "$HAVE_REPO" = 1 ]; then
  REPO="$(cd "$REPO" && pwd)"
  echo "local mode: specs are written to $REPO/e2e/qa/<TICKET>.spec.ts (not committed, not pushed)"
  export TEST_REPO_LOCAL_PATH="$REPO" TEST_REPO_NO_GIT=true GIT_PUSH_ENABLED=false
  if [ -f "$REPO/.env" ]; then export TEST_REPO_ENV_FILE="$REPO/.env"
  else warn "$REPO/.env missing - add TBA_E2E_EMAIL / TBA_E2E_PASSWORD there or generated specs cannot sign in"; fi
  if [ "$NEEDS_TEST_REPO" = 1 ]; then
    (cd "$REPO" && { [ -d node_modules ] || npm ci --no-audit --no-fund; } && npx playwright install chromium >/dev/null)
  fi
else
  # Never push unless asked.
  export GIT_PUSH_ENABLED=false
  warn "no tripbuilder-e2e clone at $REPO - generated specs cannot be written locally."
  echo "       Clone it first: git clone https://github.com/regina-triparc/tripbuilder-e2e.git \"$REPO\""
fi

# ---- Setup check ------------------------------------------------------------------
step "Setup check"
if ! "$VPY" -m taf doctor && [ "$PROVIDER" != offline ]; then
  fail "setup check failed (see above). Fix it, or prove the pipeline without a model: $SCRIPT_NAME --provider offline"
fi
