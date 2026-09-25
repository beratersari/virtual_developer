#!/usr/bin/env bash
# Offline Codex CLI only. Copies the binary and config.toml.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"
SRC="$HERE/codex/codex"
CFG="$HERE/codex/config.toml"
if [[ ! -f "$SRC" || ! -f "$CFG" ]]; then
  echo "[ERROR] Missing codex/codex or codex/config.toml" >&2
  exit 1
fi
installed="$(vd_install_binary "$SRC" codex "${HOME}/.local/bin")"
mkdir -p "${HOME}/.codex"
cp -f "$CFG" "${HOME}/.codex/config.toml"
echo "[OK] Codex CLI copied to $installed"
echo "[OK] Config copied to ${HOME}/.codex/config.toml"
echo "Set CUSTOM_HOST_TOKEN to the token for YOUR_HOST, then open a new terminal."
