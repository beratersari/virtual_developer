#!/usr/bin/env bash
# Find the CLI already on PATH (or the usual folder), rename it with the
# date at the end, then copy the new binary into that same directory.
vd_install_binary() {
  local src="$1"
  local name="$2"
  local fallback_dir="$3"
  local dest=""
  if [[ ! -f "$src" ]]; then
    echo "[ERROR] Missing new binary: $src" >&2
    return 1
  fi
  if command -v "$name" >/dev/null 2>&1; then
    dest="$(command -v "$name")"
    if [[ "$dest" == "$src" ]]; then
      dest=""
    fi
  fi
  if [[ -z "$dest" && -e "$fallback_dir/$name" ]]; then
    dest="$fallback_dir/$name"
  fi
  if [[ -z "$dest" ]]; then
    mkdir -p "$fallback_dir"
    dest="$fallback_dir/$name"
  fi
  if [[ -e "$dest" ]]; then
    local stamp bak
    stamp="$(date +%Y%m%d)"
    bak="${dest}.${stamp}"
    if [[ -e "$bak" ]]; then
      bak="${dest}.${stamp}-$(date +%H%M%S)"
    fi
    mv "$dest" "$bak"
    echo "[OK] Renamed existing ${name} to ${bak}" >&2
  fi
  mkdir -p "$(dirname "$dest")"
  cp -f "$src" "$dest"
  chmod +x "$dest"
  printf '%s\n' "$dest"
}
