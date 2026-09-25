#!/usr/bin/env bash
# Shared helpers for Linux install/start scripts. Source this file; do not exec.
# shellcheck shell=bash

vd_repo_root() {
  local here
  here="$(cd "$(dirname "${BASH_SOURCE[1]}")" && pwd)"
  if [[ -f "$here/cli.py" && -d "$here/src" ]]; then
    printf '%s\n' "$here"
    return 0
  fi
  if [[ -f "$here/../../cli.py" && -d "$here/../../src" ]]; then
    cd "$here/../.." && pwd
    return 0
  fi
  printf '%s\n' "$here"
}

vd_find_python() {
  local root="$1"
  if [[ -x "$root/.venv/bin/python" ]]; then
    printf '%s\n' "$root/.venv/bin/python"
    return 0
  fi
  local cand
  for cand in python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      if "$cand" -c 'import sys; raise SystemExit(0 if sys.version_info[:2] >= (3, 10) else 1)' \
        >/dev/null 2>&1; then
        printf '%s\n' "$(command -v "$cand")"
        return 0
      fi
    fi
  done
  return 1
}

# Find the binary already on PATH, or the usual install path. Rename the
# old file with today's date at the end, then copy the new one there.
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

vd_find_opencode() {
  local home_bin="${HOME}/.opencode/bin/opencode"
  if [[ -x "$home_bin" ]]; then
    printf '%s\n' "$home_bin"
    return 0
  fi
  if command -v opencode >/dev/null 2>&1; then
    command -v opencode
    return 0
  fi
  return 1
}

vd_wait_http() {
  local url="$1"
  local timeout="${2:-90}"
  local pattern="${3:-}"
  local elapsed=0
  local body
  while (( elapsed < timeout )); do
    body="$(curl -fsS --max-time 3 "$url" 2>/dev/null || true)"
    if [[ -n "$body" ]]; then
      if [[ -z "$pattern" ]] || grep -q -- "$pattern" <<<"$body"; then
        return 0
      fi
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  return 1
}

vd_port_listening() {
  local port="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -q ":${port} "
    return $?
  fi
  if command -v lsof >/dev/null 2>&1; then
    lsof -iTCP:"$port" -sTCP:LISTEN >/dev/null 2>&1
    return $?
  fi
  return 1
}

vd_kill_listen_port() {
  # Free a TCP listen port. Does not touch other PIDs.
  local port="$1"
  local pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)"
  elif command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
    return 0
  fi
  local pid
  for pid in $pids; do
    [[ -n "$pid" ]] || continue
    kill "$pid" >/dev/null 2>&1 || true
  done
}

vd_kill_daemon() {
  # Kill python -m src.daemon only (never serve_frontend.py).
  local root="$1"
  local pid
  if command -v pgrep >/dev/null 2>&1; then
    while read -r pid; do
      [[ -n "$pid" ]] || continue
      if [[ -r "/proc/$pid/cmdline" ]] && \
        tr '\0' ' ' <"/proc/$pid/cmdline" | grep -q 'serve_frontend.py'; then
        continue
      fi
      kill "$pid" >/dev/null 2>&1 || true
    done < <(pgrep -f "python.*-m src.daemon" || true)
  fi
}

vd_dotenv_get() {
  local file="$1"
  local key="$2"
  [[ -f "$file" ]] || return 0
  local line
  line="$(grep -E "^${key}=" "$file" 2>/dev/null | tail -n1 || true)"
  line="${line#${key}=}"
  line="${line%\"}"
  line="${line#\"}"
  line="${line%\'}"
  line="${line#\'}"
  printf '%s' "$line"
}

vd_parse_serve_url() {
  # Sets SERVE_HOST / SERVE_PORT from OPENCODE_SERVE_URL if present.
  local raw="${OPENCODE_SERVE_URL:-}"
  [[ -n "$raw" ]] || return 0
  raw="${raw#http://}"
  raw="${raw#https://}"
  raw="${raw%%/*}"
  if [[ "$raw" == *:* ]]; then
    SERVE_HOST="${raw%%:*}"
    SERVE_PORT="${raw##*:}"
  elif [[ -n "$raw" ]]; then
    SERVE_HOST="$raw"
  fi
}

vd_ensure_durable_dirs() {
  local base="${XDG_DATA_HOME:-${HOME}/.local/share}/yaver"
  mkdir -p "${base}/yaver" "${base}/t"
  echo "[OK] durable dirs ${base}/yaver and ${base}/t"
}

vd_write_opencode_config() {
  local src="$1"
  local dest_dir="${HOME}/.config/opencode"
  local home_dir="${HOME}/.opencode"
  mkdir -p "$dest_dir" "$home_dir"
  if [[ -f "$src" ]]; then
    cp -f "$src" "$dest_dir/opencode.json"
    cp -f "$src" "$home_dir/opencode.json"
  else
    printf '%s\n' '{
  "$schema": "https://opencode.ai/config.json",
  "autoupdate": false,
  "plugin": []
}' >"$dest_dir/opencode.json"
    cp -f "$dest_dir/opencode.json" "$home_dir/opencode.json"
  fi
  echo "[OK] OpenCode config plugin=[] autoupdate=false"
}
