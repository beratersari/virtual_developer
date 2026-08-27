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
  local zip="$ROOT/vendor/opencode-home.zip"
  local oc_home="${HOME}/.opencode"
  local oc_bin="$oc_home/bin"
  if [[ -f "$zip" ]]; then
    echo "Installing OpenCode from vendor/opencode-home.zip (offline)..."
    rm -rf "$oc_home"
    mkdir -p "$oc_bin"
    if command -v unzip >/dev/null 2>&1; then
      unzip -qo "$zip" -d "$oc_home"
    else
      python3 -c "import zipfile; zipfile.ZipFile(r'$zip').extractall(r'$oc_home')"
    fi
    if [[ -x "$ROOT/vendor/bin/opencode" ]]; then
      cp -f "$ROOT/vendor/bin/opencode" "$oc_bin/opencode"
    fi
    if [[ -x "$ROOT/vendor/bin/glab" ]]; then
      cp -f "$ROOT/vendor/bin/glab" "$oc_bin/glab"
    fi
    chmod +x "$oc_bin/opencode" 2>/dev/null || true
    chmod +x "$oc_bin/glab" 2>/dev/null || true
  elif OC="$(vd_find_opencode)"; then
    echo "[OK] OpenCode already present: $OC"
  else
    echo "No vendor/opencode-home.zip — installing OpenCode from the network..."
    echo "For offline install, use the CI Linux zip (workflow: Linux Distribution)."
    curl -fsSL https://opencode.ai/install | bash
  fi
  vd_write_opencode_config "$HERE/opencode.json"
  export PATH="${HOME}/.opencode/bin:${PATH}"
  if ! vd_find_opencode >/dev/null; then
    echo "[ERROR] OpenCode install finished but opencode is not on PATH."
    echo "Add ${HOME}/.opencode/bin to PATH and re-run."
    exit 1
  fi
  echo "[OK] OpenCode ready (stock build/plan agents; plugin=[])"
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
