#!/usr/bin/env bash
# Frontend only: SPA on :5173, proxies /api and /ws to backend :8080.
# Does NOT kill the daemon.
# Usage: start-frontend.sh [--daemon]
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

FRONTEND_HOST="${VD_FRONTEND_HOST:-0.0.0.0}"
FRONTEND_PORT="${VD_FRONTEND_PORT:-5173}"
BACKEND_URL="${VD_BACKEND_URL:-http://127.0.0.1:8080}"
SERVE_PY="$ROOT/packaging/windows/serve_frontend.py"
VD_WEB_DIST="${VD_WEB_DIST:-$ROOT/web/dist}"

if ! VD_PY="$(vd_find_python "$ROOT")"; then
  echo "[ERROR] No project .venv and no Python 3.10+ on PATH."
  echo "Run ./install-dashboard.sh"
  exit 1
fi

echo "========================================"
echo "  Virtual Developer - Frontend"
echo "========================================"
echo "Project  : $ROOT"
echo "UI       : http://0.0.0.0:${FRONTEND_PORT}/  (open http://127.0.0.1:${FRONTEND_PORT}/)"
echo "Proxies  : /api and /ws -> $BACKEND_URL"
echo

if [[ ! -f "$VD_WEB_DIST/index.html" ]]; then
  echo "[ERROR] Missing $VD_WEB_DIST/index.html"
  echo "Build the SPA: cd web && npm ci && npm run build"
  exit 1
fi
if [[ ! -f "$SERVE_PY" ]]; then
  echo "[ERROR] Missing $SERVE_PY"
  exit 1
fi

if ! vd_wait_http "${BACKEND_URL}/api/meta" 15; then
  echo "[ERROR] Backend is not reachable at $BACKEND_URL"
  echo "Start it first:  ./start-backend.sh"
  exit 1
fi
echo "[OK] Backend is reachable."

echo "Stopping previous frontend on port $FRONTEND_PORT only (backend stays up)..."
# Do not kill src.daemon.
if command -v lsof >/dev/null 2>&1; then
  for pid in $(lsof -t -iTCP:"$FRONTEND_PORT" -sTCP:LISTEN 2>/dev/null || true); do
    if [[ -r "/proc/$pid/cmdline" ]] && \
      tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q 'src.daemon'; then
      continue
    fi
    kill "$pid" >/dev/null 2>&1 || true
  done
fi
sleep 1

if ! vd_wait_http "${BACKEND_URL}/api/meta" 10; then
  echo "[ERROR] Backend died or is unreachable after frontend cleanup."
  echo "Re-run ./start-backend.sh, then ./start-frontend.sh"
  exit 1
fi
echo "[OK] Backend still up."

export VD_WEB_DIST
export VD_BACKEND_URL="$BACKEND_URL"
export VD_FRONTEND_HOST="$FRONTEND_HOST"
export VD_FRONTEND_PORT="$FRONTEND_PORT"

if [[ "$BACKGROUND" -eq 1 ]]; then
  mkdir -p "$ROOT/logs"
  nohup "$VD_PY" "$SERVE_PY" --dist "$VD_WEB_DIST" --backend "$BACKEND_URL" \
    --host "$FRONTEND_HOST" --port "$FRONTEND_PORT" >>"$ROOT/logs/frontend.log" 2>&1 &
  echo $! >"$ROOT/logs/frontend.pid"
  if ! vd_wait_http "http://127.0.0.1:${FRONTEND_PORT}/" 60 "id=\"root\""; then
    echo "[ERROR] Frontend did not become ready on port $FRONTEND_PORT."
    echo "See $ROOT/logs/frontend.log"
    exit 1
  fi
  echo
  echo "[OK] Frontend is up: http://127.0.0.1:${FRONTEND_PORT}/"
  exit 0
fi

echo "Starting frontend in the foreground (Ctrl+C to stop)..."
exec "$VD_PY" "$SERVE_PY" --dist "$VD_WEB_DIST" --backend "$BACKEND_URL" \
  --host "$FRONTEND_HOST" --port "$FRONTEND_PORT"
