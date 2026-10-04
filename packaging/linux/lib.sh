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

# Last matching KEY wins. Same quotes and escapes as python-dotenv for one line.
# Prints the value and returns 0 when the key exists, including an empty value.
vd_dotenv_decode() {
  local text="$1" mode="$2" out="" i=0 n c nxt
  n=${#text}
  while (( i < n )); do
    c="${text:i:1}"
    if [[ "$c" == '\' && $((i + 1)) -lt $n ]]; then
      nxt="${text:i+1:1}"
      if [[ "$nxt" == '\' ]]; then
        out+='\'
        i=$((i + 2))
        continue
      fi
      if [[ "$nxt" == "'" ]]; then
        out+="'"
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == '"' ]]; then
        out+='"'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "n" ]]; then
        out+=$'\n'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "r" ]]; then
        out+=$'\r'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "t" ]]; then
        out+=$'\t'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "a" ]]; then
        out+=$'\a'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "b" ]]; then
        out+=$'\b'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "f" ]]; then
        out+=$'\f'
        i=$((i + 2))
        continue
      fi
      if [[ "$mode" == "double" && "$nxt" == "v" ]]; then
        out+=$'\v'
        i=$((i + 2))
        continue
      fi
    fi
    out+="$c"
    i=$((i + 1))
  done
  vd_dot_value="$out"
}

vd_dotenv_unquote() {
  local s="$1" q n=0 i=1 buf="" esc=0 c mode
  vd_dot_value=""
  q="${s:0:1}"
  n=${#s}
  while (( i < n )); do
    c="${s:i:1}"
    if (( esc )); then
      buf+="\\$c"
      esc=0
      i=$((i + 1))
      continue
    fi
    if [[ "$c" == '\' ]]; then
      esc=1
      i=$((i + 1))
      continue
    fi
    if [[ "$c" == "$q" ]]; then
      mode="single"
      [[ "$q" == '"' ]] && mode="double"
      vd_dotenv_decode "$buf" "$mode"
      return 0
    fi
    buf+="$c"
    i=$((i + 1))
  done
  return 1
}

vd_dotenv_unquoted() {
  local s="$1"
  if [[ "$s" =~ [[:space:]]+# ]]; then
    s="${s%%[[:space:]]#*}"
  fi
  s="${s%"${s##*[![:space:]]}"}"
  vd_dot_value="$s"
}

vd_dotenv_key() {
  local file="$1" key="$2"
  [[ -f "$file" ]] || return 1
  local line trimmed rest name found=0 val=""
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    line="${line#$'\ufeff'}"
    trimmed="${line#"${line%%[![:space:]]*}"}"
    [[ -z "$trimmed" || "$trimmed" == \#* ]] && continue
    if [[ "$trimmed" == export[[:space:]]* ]]; then
      trimmed="${trimmed#export}"
      trimmed="${trimmed#"${trimmed%%[![:space:]]*}"}"
    fi
    [[ "$trimmed" == *=* ]] || continue
    name="${trimmed%%=*}"
    name="${name%"${name##*[![:space:]]}"}"
    [[ "$name" == "$key" ]] || continue
    rest="${trimmed#*=}"
    rest="${rest#"${rest%%[![:space:]]*}"}"
    if [[ "${rest:0:1}" == '"' || "${rest:0:1}" == "'" ]]; then
      if vd_dotenv_unquote "$rest"; then
        found=1
        val="$vd_dot_value"
      fi
    else
      vd_dotenv_unquoted "$rest"
      found=1
      val="$vd_dot_value"
    fi
  done <"$file"
  [[ "$found" -eq 1 ]] || return 1
  printf '%s' "$val"
}

vd_dotenv_get() {
  local val=""
  if val="$(vd_dotenv_key "$1" "$2")"; then
    printf '%s' "$val"
  fi
  return 0
}

vd_import_opencode_serve_auth() {
  local file="$1" key val
  [[ -f "$file" ]] || return 0
  for key in OPENCODE_SERVER_PASSWORD OPENCODE_SERVER_USERNAME; do
    if [[ -n "${!key+x}" ]]; then
      continue
    fi
    if val="$(vd_dotenv_key "$file" "$key")"; then
      printf -v "$key" '%s' "$val"
      export "$key"
    fi
  done
  return 0
}

vd_serve_auth_header() {
  local pass="${OPENCODE_SERVER_PASSWORD-}"
  [[ -n "$pass" ]] || return 0
  local user="${OPENCODE_SERVER_USERNAME:-opencode}"
  [[ -n "$user" ]] || user="opencode"
  printf 'Basic %s' "$(printf '%s' "${user}:${pass}" | base64 | tr -d '\n\r')"
}

vd_serve_http_code=""

vd_curl_native_path() {
  # Git Bash curl is a Windows binary and cannot open /tmp paths.
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
    return 0
  fi
  printf '%s' "$1"
}

vd_wait_serve_health() {
  local url="$1"
  local timeout="${2:-90}"
  local elapsed=0
  local header="" code="" tmp="" cfg="" tmp_arg="" cfg_arg=""
  vd_serve_http_code=""
  header="$(vd_serve_auth_header || true)"
  tmp="$(mktemp)"
  cfg="$(mktemp)"
  chmod 600 "$cfg" 2>/dev/null || true
  tmp_arg="$(vd_curl_native_path "$tmp")"
  cfg_arg="$(vd_curl_native_path "$cfg")"
  # The URL and the password header stay in the config file so they are not
  # rewritten as filesystem paths and do not appear on the curl command line.
  {
    printf 'url = "%s"\n' "$url"
    printf 'output = "%s"\n' "$tmp_arg"
    printf 'silent\n'
    printf 'show-error\n'
    printf 'max-time = 3\n'
    if [[ -n "$header" ]]; then
      printf 'header = "Authorization: %s"\n' "$header"
    fi
  } >"$cfg"
  while (( elapsed < timeout )); do
    code="$(curl --config "$cfg_arg" -w "%{http_code}" 2>/dev/null || true)"
    code="$(printf '%s' "$code" | tr -d '[:space:]')"
    if [[ "$code" == "401" ]]; then
      rm -f "$tmp" "$cfg"
      vd_serve_http_code=401
      echo "HTTP 401 from $url (server answered; authorization missing or wrong)" >&2
      return 3
    fi
    if [[ "$code" =~ ^[0-9]+$ ]] && (( code >= 200 && code < 500 )); then
      if grep -qi -- "healthy" "$tmp"; then
        rm -f "$tmp" "$cfg"
        return 0
      fi
    fi
    sleep 1
    elapsed=$((elapsed + 1))
  done
  rm -f "$tmp" "$cfg"
  return 1
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
