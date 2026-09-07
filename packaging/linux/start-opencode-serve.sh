#!/usr/bin/env bash
# Force-restart OpenCode serve on :4096 (or OPENCODE_SERVE_URL).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

SERVE_PORT="${OPENCODE_SERVE_PORT:-4096}"
if [[ -f "$ROOT/.env" ]]; then
  OPENCODE_SERVE_URL="$(vd_dotenv_get "$ROOT/.env" OPENCODE_SERVE_URL)"
  export OPENCODE_SERVE_URL
fi
vd_parse_serve_url

echo "Stopping previous OpenCode serve on port $SERVE_PORT..."
if command -v pgrep >/dev/null 2>&1; then
  while read -r pid; do
    [[ -n "$pid" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done < <(pgrep -f "opencode serve" || true)
fi
vd_kill_listen_port "$SERVE_PORT"
sleep 1

exec "$HERE/ensure-opencode-serve.sh" 90
