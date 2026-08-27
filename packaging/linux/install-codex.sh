#!/usr/bin/env bash
# Codex CLI only. Does not touch OpenCode or the Python venv.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$HERE/lib.sh"

ROOT="${VD_REPO_ROOT:-$(cd "$HERE/../.." && pwd)}"
cd "$ROOT"

echo "========================================"
echo "  Virtual Developer - Codex install"
echo "========================================"
echo

DEST_DIR="${HOME}/.local/bin"
mkdir -p "$DEST_DIR"

vendor_pkg="$(ls "$ROOT"/vendor/codex-*.tar.gz 2>/dev/null | head -n1 || true)"
if [[ -n "$vendor_pkg" && -f "$vendor_pkg" ]]; then
  echo "Installing Codex from $vendor_pkg (offline)..."
  tmp="$(mktemp -d)"
  tar -xzf "$vendor_pkg" -C "$tmp"
  bin="$(find "$tmp" -type f \( -name 'codex' -o -name 'codex-*linux*' \) -print -quit || true)"
  if [[ -z "$bin" && -x "$ROOT/vendor/bin/codex" ]]; then
    bin="$ROOT/vendor/bin/codex"
  fi
  if [[ -z "$bin" || ! -f "$bin" ]]; then
    echo "[ERROR] No codex binary in $vendor_pkg"
    exit 1
  fi
  cp -f "$bin" "$DEST_DIR/codex"
  chmod +x "$DEST_DIR/codex"
  rm -rf "$tmp"
  export PATH="$DEST_DIR:$PATH"
elif [[ -x "$ROOT/vendor/bin/codex" ]]; then
  echo "Installing Codex from vendor/bin/codex (offline)..."
  cp -f "$ROOT/vendor/bin/codex" "$DEST_DIR/codex"
  chmod +x "$DEST_DIR/codex"
  export PATH="$DEST_DIR:$PATH"
elif command -v codex >/dev/null 2>&1; then
  echo "[OK] Codex already on PATH: $(command -v codex)"
else
  echo "No vendor Codex archive — installing from the network..."
  echo "For offline install, use the CI Linux zip (workflow: Linux Distribution)."
  if command -v npm >/dev/null 2>&1; then
    npm install -g @openai/codex
  else
    echo "[ERROR] Could not install Codex automatically."
    echo "Download the Linux zip from GitHub Actions, or install: npm i -g @openai/codex"
    exit 1
  fi
fi

if ! command -v codex >/dev/null 2>&1; then
  echo "[ERROR] Codex is still not on PATH after install."
  exit 1
fi

CFG_DIR="${HOME}/.codex"
mkdir -p "$CFG_DIR"
if [[ ! -f "$CFG_DIR/config.toml" ]]; then
  if [[ -f "$HERE/../windows/codex-config.toml" ]]; then
    cp -f "$HERE/../windows/codex-config.toml" "$CFG_DIR/config.toml"
  else
    printf '%s\n' '# Seeded by Virtual Developer install-codex.sh
model = "gpt-5"
approval_policy = "untrusted"
sandbox_mode = "workspace-write"
' >"$CFG_DIR/config.toml"
  fi
  echo "[OK] Seeded $CFG_DIR/config.toml (run: codex login)"
else
  echo "[OK] $CFG_DIR/config.toml already exists (left unchanged)"
fi

echo
echo "[OK] Codex: $(command -v codex)"
echo "Next: install-dashboard.sh if the app is not installed yet."
echo
