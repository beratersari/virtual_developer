#!/usr/bin/env bash
# Dashboard-only installer: Python .venv + deps + .env + cli.py init.
# Does not install OpenCode / Codex. Use install-backends.sh / install-codex.sh.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="${VD_REPO_ROOT:-$(cd "$HERE/../.." && pwd)}"
cd "$ROOT"

echo "========================================"
echo "  Virtual Developer - Dashboard install"
echo "  Backend + frontend only (no OpenCode)"
echo "========================================"
echo
echo "Install root : $ROOT"
echo

if ! PY="$(vd_find_python "$ROOT")"; then
  echo "[ERROR] Python 3.10+ is required (prefer 3.12)."
  echo "Install python3 and python3-venv, then re-run."
  exit 1
fi
echo "[OK] $($PY --version 2>&1)"

echo
echo "Step 1: Python virtual environment..."
if [[ ! -x "$ROOT/.venv/bin/python" ]]; then
  "$PY" -m venv "$ROOT/.venv"
  echo "[OK] Created $ROOT/.venv"
else
  echo "[OK] Using existing $ROOT/.venv"
fi
VENV_PY="$ROOT/.venv/bin/python"
VENV_PIP="$ROOT/.venv/bin/pip"

echo
echo "Step 2: Installing Python packages..."
if [[ -d "$ROOT/vendor/python-wheels" ]]; then
  echo "Using offline wheels from vendor/python-wheels..."
  "$VENV_PIP" install --upgrade pip --no-index --find-links="$ROOT/vendor/python-wheels" >/dev/null 2>&1 || true
  "$VENV_PIP" install --no-index --find-links="$ROOT/vendor/python-wheels" -r "$ROOT/requirements.txt"
else
  echo "[INFO] No vendor/python-wheels — installing from PyPI..."
  "$VENV_PIP" install --upgrade pip --quiet
  "$VENV_PIP" install -r "$ROOT/requirements.txt"
fi
echo "[OK] Python dependencies installed into .venv"

echo
echo "Step 3: Durable host dirs..."
vd_ensure_durable_dirs

if [[ -f "$ROOT/web/dist/index.html" ]]; then
  echo "[OK] ops dashboard SPA present: web/dist"
else
  echo "[WARNING] web/dist/index.html missing — UI on :8080 will be JSON-only"
  echo "          From source: cd web && npm ci && npm run build"
fi

echo
echo "Step 4: Project configuration..."
if [[ ! -f "$ROOT/.env" ]]; then
  if [[ -f "$ROOT/.env.example" ]]; then
    cp -f "$ROOT/.env.example" "$ROOT/.env"
    echo "[OK] Created .env from .env.example — edit credentials before start"
  else
    echo "[WARNING] .env.example missing; create .env manually"
  fi
else
  echo "[OK] .env already exists (left unchanged)"
fi

echo
echo "Step 5: Initializing project (cli.py init)..."
if ! "$VENV_PY" cli.py init; then
  echo "[WARNING] cli.py init reported issues; continuing..."
else
  echo "[OK] Project initialized"
fi

echo
echo "Step 6: Checking for OpenCode / glab..."
if OC="$(vd_find_opencode)"; then
  echo "[OK] Found opencode: $OC"
else
  echo "[WARNING] opencode not found. Agent jobs need ./install-backends.sh"
fi
if command -v glab >/dev/null 2>&1; then
  echo "[OK] Found glab: $(command -v glab)"
else
  echo "[INFO] glab not on PATH — MR create may use the GitLab API instead"
fi

echo
echo "========================================"
echo "  Dashboard install complete"
echo "========================================"
echo
echo "Python venv : $ROOT/.venv"
echo
echo "Next:"
echo "  1. Edit .env (JIRA_HOST, JIRA_API_TOKEN, JIRA_BOARD_ID, ...)"
echo "  2. ./start-backend.sh     API + SPA on http://127.0.0.1:8080/"
echo "     ./start-frontend.sh    UI on http://127.0.0.1:5173/"
echo "     ./start.sh             both"
echo
echo "Agent workers:"
echo "  ./install-backends.sh     OpenCode (+ Codex if no args)"
echo "  ./install-codex.sh        Codex only"
echo
