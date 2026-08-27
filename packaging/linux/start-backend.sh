#!/usr/bin/env bash
# Start daemon + API on :8080. Ensures OpenCode serve on :4096 first.
# SPA is served by the daemon at http://0.0.0.0:8080/ when web/dist exists.
# Usage: start-backend.sh [--daemon]
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

BACKGROUND=0
if [[ "${1:-}" == "--daemon" || "${1:-}" == "-d" ]]; then
  BACKGROUND=1
fi

DASH_PORT="${DASHBOARD_PORT:-8080}"
SERVE_PORT="${OPENCODE_SERVE_PORT:-4096}"

if ! VD_PY="$(vd_find_python "$ROOT")"; then
  echo "[ERROR] No project .venv and no Python 3.10+ on PATH."
  echo "Run ./install-dashboard.sh"
  exit 1
fi

export DASHBOARD_HOST="${DASHBOARD_HOST:-0.0.0.0}"
export DASHBOARD_ALLOW_REMOTE="${DASHBOARD_ALLOW_REMOTE:-true}"
export DASHBOARD_PORT="$DASH_PORT"
export DASHBOARD_ENABLED="${DASHBOARD_ENABLED:-true}"
export VD_WEB_DIST="${VD_WEB_DIST:-$ROOT/web/dist}"
export GIT_TERMINAL_PROMPT=0
export OPENCODE_DISABLE_MODELS_FETCH=1
export PATH="${HOME}/.opencode/bin:${PATH}"

echo "========================================"
echo "  Virtual Developer - Backend"
echo "========================================"
echo "Project : $ROOT"
echo "API+SPA : http://0.0.0.0:${DASH_PORT}/  (open http://127.0.0.1:${DASH_PORT}/)"
echo "Python  : $VD_PY"
echo

if [[ ! -f "$ROOT/.env" && -f "$ROOT/.env.example" ]]; then
  cp -f "$ROOT/.env.example" "$ROOT/.env"
  echo "[OK] Created .env from .env.example"
fi

if [[ ! -f "$VD_WEB_DIST/index.html" ]]; then
  echo "[WARNING] web/dist/index.html missing — :8080 will serve JSON only."
fi

"$HERE/ensure-opencode-serve.sh" 90

echo "Stopping previous backend (does not stop serve or frontend)..."
vd_kill_daemon "$ROOT"
vd_kill_listen_port "$DASH_PORT"
sleep 1

LOG_DIR="$ROOT/logs"
mkdir -p "$LOG_DIR"

if [[ "$BACKGROUND" -eq 1 ]]; then
  nohup "$VD_PY" -m src.daemon >>"$LOG_DIR/backend.log" 2>&1 &
  echo $! >"$LOG_DIR/backend.pid"
  if ! vd_wait_http "http://127.0.0.1:${DASH_PORT}/api/meta" 90; then
    echo "[ERROR] Backend did not become ready on port $DASH_PORT."
    echo "See $LOG_DIR/backend.log"
    exit 1
  fi
  echo
  echo "[OK] Backend is up."
  echo "  Dashboard: http://127.0.0.1:${DASH_PORT}/"
  echo "  API meta : http://127.0.0.1:${DASH_PORT}/api/meta"
  echo "  Serve    : http://127.0.0.1:${SERVE_PORT}/global/health"
  exit 0
fi

echo "Starting daemon in the foreground (Ctrl+C to stop)..."
echo
exec "$VD_PY" -m src.daemon
