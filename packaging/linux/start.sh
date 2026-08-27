#!/usr/bin/env bash
# Start backend (and SPA on :8080) plus optional frontend proxy on :5173.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

echo "========================================"
echo "  Virtual Developer - start"
echo "========================================"
echo

"$HERE/start-backend.sh" --daemon
if [[ -f "$ROOT/web/dist/index.html" ]]; then
  "$HERE/start-frontend.sh" --daemon || true
fi

echo
echo "[OK] Product is up."
echo "  Backend / SPA : http://127.0.0.1:8080/"
echo "  Frontend      : http://127.0.0.1:5173/  (if web/dist is present)"
echo "  Stop          : ./stop.sh"
echo
