#!/usr/bin/env bash
# Stop dashboard daemon and frontend. Does not kill shared opencode serve
# unless --serve is passed.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../.." && pwd)"
KILL_SERVE=0
if [[ "${1:-}" == "--serve" ]]; then
  KILL_SERVE=1
fi

echo "Stopping Yaver daemon..."
vd_kill_daemon "$ROOT"
vd_kill_listen_port "${DASHBOARD_PORT:-8080}"

echo "Stopping frontend on :5173 (daemon already stopped if it shared that port)..."
if command -v pgrep >/dev/null 2>&1; then
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done < <(pgrep -f "serve_frontend.py" || true)
fi
vd_kill_listen_port "${VD_FRONTEND_PORT:-5173}"

if [[ "$KILL_SERVE" -eq 1 ]]; then
  echo "Stopping OpenCode serve..."
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done < <(pgrep -f "opencode serve" || true)
  vd_kill_listen_port "${OPENCODE_SERVE_PORT:-4096}"
fi

rm -f "$ROOT/logs/backend.pid" "$ROOT/logs/frontend.pid" 2>/dev/null || true
echo "[OK] Stopped."
