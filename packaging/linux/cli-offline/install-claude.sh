#!/usr/bin/env bash
# Offline Claude Code CLI only. Copies the binary and settings.json.
# Does not copy agents or skills.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
SRC="$HERE/claude/claude"
CFG="$HERE/claude/settings.json"
if [[ ! -f "$SRC" || ! -f "$CFG" ]]; then
  echo "[ERROR] Missing claude/claude or claude/settings.json" >&2
  exit 1
fi
installed="$(vd_install_binary "$SRC" claude "${HOME}/.local/bin")"
mkdir -p "${HOME}/.claude"
cp -f "$CFG" "${HOME}/.claude/settings.json"
echo "[OK] Claude Code CLI copied to $installed"
echo "[OK] Config copied to ${HOME}/.claude/settings.json"
echo "Edit YOUR_HOST in claude/settings.json and run this script again to replace the config."
echo "Agents are not copied. Use install-agents.sh from the Yaver zip."
