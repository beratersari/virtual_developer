#!/usr/bin/env bash
# Copy opencoderman/agents and opencoderman/skills into the OpenCode home.
# Does not install the OpenCode CLI. Never writes ~/.config/opencode.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SRC_AGENTS="$HERE/opencoderman/agents"
SRC_SKILLS="$HERE/opencoderman/skills"

if [[ ! -f "$SRC_AGENTS/derman-build.md" || ! -f "$SRC_AGENTS/derman-plan.md" || ! -d "$SRC_SKILLS" ]]; then
  echo "[ERROR] opencoderman/agents and opencoderman/skills not found next to this script." >&2
  echo "        Expected: $SRC_AGENTS" >&2
  echo "                  $SRC_SKILLS" >&2
  exit 1
fi

home_from_binary() {
  local bin_dir parent leaf
  bin_dir="$(cd "$(dirname "$1")" && pwd)"
  leaf="$(basename "$bin_dir")"
  if [[ "$leaf" == "bin" ]]; then
    parent="$(cd "$bin_dir/.." && pwd)"
    echo "$parent"
    return 0
  fi
  echo "$bin_dir"
}

looks_like_home() {
  local d="$1"
  [[ -d "$d" ]] || return 1
  [[ -x "$d/bin/opencode" || -f "$d/bin/opencode" || -f "$d/opencode.json" || -d "$d/agents" ]]
}

OC_HOME=""
if [[ -n "${OPENCODE_HOME:-}" && -d "$OPENCODE_HOME" ]]; then
  OC_HOME="$OPENCODE_HOME"
elif looks_like_home "${HOME}/.opencode"; then
  OC_HOME="${HOME}/.opencode"
else
  BIN="$(command -v opencode || true)"
  if [[ -n "$BIN" ]]; then
    CAND="$(home_from_binary "$BIN")"
    if looks_like_home "$CAND"; then
      OC_HOME="$CAND"
    fi
  fi
fi

if [[ -z "$OC_HOME" ]]; then
  echo "[ERROR] OpenCode is not installed." >&2
  echo "        Set OPENCODE_HOME, or install OpenCode so ~/.opencode exists." >&2
  exit 1
fi

echo "OpenCode home : $OC_HOME"
echo "Source agents : $SRC_AGENTS"
echo "Source skills : $SRC_SKILLS"

mkdir -p "$OC_HOME/agents" "$OC_HOME/skills"
cp -a "$SRC_AGENTS/." "$OC_HOME/agents/"
cp -a "$SRC_SKILLS/." "$OC_HOME/skills/"

if [[ ! -f "$OC_HOME/agents/derman-build.md" || ! -f "$OC_HOME/agents/derman-plan.md" ]]; then
  echo "[ERROR] Copy finished but derman-build.md / derman-plan.md is missing." >&2
  exit 1
fi

echo "[OK] agents -> $OC_HOME/agents"
echo "[OK] skills -> $OC_HOME/skills"
