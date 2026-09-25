#!/usr/bin/env bash
# Offline OpenCode CLI only. Copies the binary and opencode.json.
# Does not copy agents or skills.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
SRC="$HERE/opencode/opencode"
CFG="$HERE/opencode/opencode.json"
if [[ ! -f "$SRC" || ! -f "$CFG" ]]; then
  echo "[ERROR] Missing opencode/opencode or opencode/opencode.json" >&2
  exit 1
fi
installed="$(vd_install_binary "$SRC" opencode "${HOME}/.opencode/bin")"
mkdir -p "${HOME}/.opencode"
cp -f "$CFG" "${HOME}/.opencode/opencode.json"
if [[ -n "${HOME}" && ":$PATH:" != *":$(dirname "$installed"):"* ]]; then
  echo "Add $(dirname "$installed") to PATH."
fi
echo "[OK] OpenCode CLI copied to $installed"
echo "[OK] Config copied to ${HOME}/.opencode/opencode.json"
echo "Edit YOUR_HOST in opencode/opencode.json and run this script again to replace the config."
echo "Agents are not copied. Use install-agents.sh from the Yaver zip."
