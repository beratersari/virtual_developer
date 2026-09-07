#!/usr/bin/env bash
# Launch OpenCode TUI from the project directory — never from $HOME.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="$(cd "$HERE/../.." && pwd)"
cd "$ROOT"

export OPENCODE_DISABLE_MODELS_FETCH=1
export PATH="${HOME}/.opencode/bin:${PATH}"

if ! OC="$(vd_find_opencode)"; then
  echo "[ERROR] OpenCode not installed."
  echo "Run ./install-backends.sh"
  exit 1
fi

echo "Starting OpenCode in project folder:"
echo "  $ROOT"
echo
echo "Tip: leave this terminal open. Do not run opencode from your home directory."
echo
exec "$OC" "$@"
