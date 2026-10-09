#!/usr/bin/env bash
# Probe OpenCode serve. Healthy -> leave it. Else start it in the project dir.
# Does not kill the Yaver daemon.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

SERVE_HOST="${OPENCODE_SERVE_HOST:-127.0.0.1}"
SERVE_PORT="${OPENCODE_SERVE_PORT:-4096}"
TIMEOUT="${1:-90}"

if [[ -f "$ROOT/.env" ]]; then
  OPENCODE_SERVE_URL="$(vd_dotenv_get "$ROOT/.env" OPENCODE_SERVE_URL)"
  export OPENCODE_SERVE_URL
fi
vd_parse_serve_url
vd_import_opencode_serve_auth "$ROOT/.env"

HEALTH="http://127.0.0.1:${SERVE_PORT}/global/health"
vd_serve_http_code=""
if vd_wait_serve_health "$HEALTH" 2; then
  echo "[OK] OpenCode serve already healthy on port $SERVE_PORT"
  exit 0
fi
if [[ "$vd_serve_http_code" == "401" ]]; then
  echo "[ERROR] OpenCode serve rejected the password (HTTP 401)." >&2
  exit 1
fi

if ! OC="$(vd_find_opencode)"; then
  echo "[ERROR] OpenCode not installed."
  echo "Run ./install-backends.sh"
  exit 1
fi

if vd_port_listening "$SERVE_PORT"; then
  echo "Port $SERVE_PORT is in use; waiting for OpenCode serve health..."
else
  LOG_DIR="${HOME}/yaver_data/yaver/logs"
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/opencode-serve.log"
  echo "Starting OpenCode serve in $ROOT (log $LOG)..."
  export OPENCODE_DISABLE_MODELS_FETCH=1
  export GIT_TERMINAL_PROMPT=0
  export PATH="$(dirname "$OC"):${PATH}"
  nohup "$OC" serve --port "$SERVE_PORT" --hostname "$SERVE_HOST" \
    --print-logs --log-level INFO >>"$LOG" 2>&1 &
  echo $! >"$LOG_DIR/opencode-serve.pid"
fi

vd_serve_http_code=""
if ! vd_wait_serve_health "$HEALTH" "$TIMEOUT"; then
  if [[ "$vd_serve_http_code" == "401" ]]; then
    echo "[ERROR] OpenCode serve rejected the password (HTTP 401)." >&2
  else
    echo "[ERROR] OpenCode serve did not become healthy on $HEALTH" >&2
  fi
  exit 1
fi
echo "[OK] OpenCode serve healthy on port $SERVE_PORT"
exit 0
