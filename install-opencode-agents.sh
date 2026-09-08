#!/usr/bin/env bash
# Copy OpenCoderman agents/ and skills/ into the detected OpenCode home.
# Does not install the OpenCode CLI. Never writes ~/.config/opencode.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="$HERE/packaging/install_opencode_agents.py"
if [[ ! -f "$PY" ]]; then
  PY="$HERE/install_opencode_agents.py"
fi

if [[ -f "$PY" ]]; then
  if command -v python3 >/dev/null 2>&1; then
    exec python3 "$PY" --source-root "$HERE" "$@"
  fi
  if command -v python >/dev/null 2>&1; then
    exec python "$PY" --source-root "$HERE" "$@"
  fi
fi

echo "[ERROR] Python 3 is required to run install-opencode-agents.sh" >&2
echo "        (packaging/install_opencode_agents.py)." >&2
exit 1
