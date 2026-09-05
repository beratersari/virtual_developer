#!/usr/bin/env bash
# Agent backends only. Does not create .venv or start the dashboard.
# Usage:
#   install-backends.sh              OpenCode + Codex
#   install-backends.sh opencode     OpenCode only
#   install-backends.sh codex        Codex only
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="${VD_REPO_ROOT:-$(cd "$HERE/../.." && pwd)}"
cd "$ROOT"
WHICH="${1:-}"

echo "========================================"
echo "  Virtual Developer - Backends only"
echo "  OpenCode / Codex (no Python / dashboard)"
echo "========================================"
echo
echo "Project : $ROOT"
case "$WHICH" in
  opencode) echo "Mode    : OpenCode only" ;;
  codex) echo "Mode    : Codex only" ;;
  *) echo "Mode    : OpenCode + Codex" ;;
esac
echo

install_opencode() {
  local py ocm vendor installer online_flag=""
  if ! py="$(vd_find_python "$ROOT")"; then
    if ! py="$(vd_find_python "$(cd "$HERE/../.." && pwd)")"; then
      echo "[ERROR] Python 3.10+ is required to run opencoderman/install.py."
      exit 1
    fi
  fi
  vendor="$ROOT/vendor"
  installer="$HERE/../install_opencode.py"
  if [[ -n "${OPENCODERMAN_ROOT:-}" && -f "${OPENCODERMAN_ROOT}/install.py" ]]; then
    ocm="$OPENCODERMAN_ROOT"
  elif [[ -f "$ROOT/opencoderman/install.py" ]]; then
    ocm="$ROOT/opencoderman"
  else
    ocm="$(cd "$HERE/../.." && pwd)/opencoderman"
  fi
  if [[ ! -f "$ocm/install.py" ]]; then
    echo "[ERROR] opencoderman/install.py missing at $ocm"
    echo "From a git checkout: git submodule update --init --recursive"
    echo "From a CI zip: re-extract so the opencoderman/ folder is present."
    exit 1
  fi
  if [[ ! -f "$installer" ]]; then
    echo "[ERROR] missing $installer"
    exit 1
  fi
  if [[ -f "$ocm/vendor/bin/linux/opencode" || -x "$vendor/bin/opencode" || -f "$vendor/opencode-home.zip" ]]; then
    echo "Installing OpenCode via opencoderman (offline CLI from vendor/ or opencode-home.zip)..."
  else
    echo "No vendored OpenCode CLI — fetching via opencoderman (network)."
    echo "For offline install, use the CI Linux zip (workflow: Linux Distribution)."
    online_flag="--online"
  fi
  "$py" "$installer" \
    --repo-root "$ROOT" \
    --opencoderman-root "$ocm" \
    --vendor-root "$vendor" \
    --require-binary \
    $online_flag
  export PATH="${HOME}/.opencode/bin:${PATH}"
  if ! vd_find_opencode >/dev/null; then
    echo "[ERROR] OpenCode install finished but opencode is not on PATH."
    echo "Add ${HOME}/.opencode/bin to PATH and re-run."
    exit 1
  fi
  echo "[OK] OpenCode ready via opencoderman (stock build/plan; plugin=[])"
}

install_codex() {
  "$HERE/install-codex.sh"
}

if [[ "$WHICH" == "codex" ]]; then
  install_codex
elif [[ "$WHICH" == "opencode" ]]; then
  install_opencode
else
  install_opencode
  install_codex
fi

echo
echo "Next: ./install-dashboard.sh if the app is not installed yet."
echo "Then ./start-backend.sh"
echo "Add to your shell profile if needed:"
echo "  export PATH=\"\$HOME/.opencode/bin:\$PATH\""
echo
