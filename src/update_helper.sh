#!/bin/sh
# Replace a Yaver executable folder. The argument is a shell plan, not JSON.
# Used only when the frozen install has no Python outside that folder.
set -eu
PLAN_FILE=${1:-}
if [ -z "$PLAN_FILE" ] || [ ! -f "$PLAN_FILE" ]; then
  echo "usage: update_helper.sh PLAN.env" >&2
  exit 2
fi
# shellcheck disable=SC1090
. "$PLAN_FILE"

log_line() {
  if [ -n "${LOG:-}" ]; then
    printf '%s %s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$1" >> "$LOG"
  fi
}

write_result() {
  if [ -z "${RESULT:-}" ]; then
    return
  fi
  printf '%s\n' "$1" > "$RESULT"
}

fail() {
  log_line "$1"
  write_result "{\"ok\": false, \"version\": \"${VERSION:-}\", \"error\": \"$1\"}"
  exit 1
}

if [ "${LAYOUT:-}" != "frozen" ]; then
  fail "This helper only replaces an executable install."
fi
case "${INSTALL:-}" in
  ""|"/") fail "Refusing to replace a drive root." ;;
esac
if [ -e "$INSTALL/.git" ]; then
  fail "Refusing to replace a git checkout."
fi
if [ ! -d "${STAGING:-}" ]; then
  fail "The staged release is missing."
fi

pid_value=${PID:-0}
if [ "$pid_value" -gt 0 ]; then
  waited=0
  while [ "$waited" -lt 90 ]; do
    if ! kill -0 "$pid_value" 2>/dev/null; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if kill -0 "$pid_value" 2>/dev/null; then
    kill "$pid_value" 2>/dev/null || true
    sleep 2
    if kill -0 "$pid_value" 2>/dev/null; then
      kill -9 "$pid_value" 2>/dev/null || true
    fi
  fi
  if kill -0 "$pid_value" 2>/dev/null; then
    fail "Yaver is still running."
  fi
fi

case "${PROBE:-127.0.0.1}" in
  *[!A-Za-z0-9.:-]*) fail "The update plan has a bad dashboard address." ;;
esac
PROBE=${PROBE:-127.0.0.1}

port_open() {
  probe_host=$1
  port_num=$2
  # /dev/tcp has no connect timeout. An address that does not answer would
  # sit here until the kernel gives up and the health budget would be a lie.
  if command -v bash >/dev/null 2>&1; then
    PROBE_HOST=$probe_host PORT_NUM=$port_num bash -c 'exec 3<>"/dev/tcp/$PROBE_HOST/$PORT_NUM"' >/dev/null 2>&1 &
    probe_pid=$!
    ticks=0
    while [ "$ticks" -lt 30 ]; do
      if ! kill -0 "$probe_pid" 2>/dev/null; then
        wait "$probe_pid" || return $?
        return 0
      fi
      sleep 0.1
      ticks=$((ticks + 1))
    done
    kill "$probe_pid" 2>/dev/null || true
    wait "$probe_pid" 2>/dev/null || true
    return 1
  fi
  if command -v nc >/dev/null 2>&1; then
    nc -z -w 1 "$probe_host" "$port_num" >/dev/null 2>&1 || return $?
    return 0
  fi
  return 1
}

parent=$(dirname "$INSTALL")
leaf=$(basename "$INSTALL")
previous="$parent/$leaf.previous"
env_copy=""
if [ -f "$INSTALL/.env" ]; then
  env_copy=$(mktemp)
  cp -p "$INSTALL/.env" "$env_copy"
fi

# A data folder inside the executable folder would leave with the old tree.
userdata_rel=""
userdata_stash="$parent/$leaf.userdata"
if [ -n "$env_copy" ] && [ -f "$env_copy" ]; then
  base_dir=$(grep -E '^YAVER_BASE_DIR=' "$env_copy" | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)
  base_dir=${base_dir#"${base_dir%%[![:space:]]*}"}
  base_dir=${base_dir%"${base_dir##*[![:space:]]}"}
  case "$base_dir" in
    \"*\") base_dir=${base_dir#\"}; base_dir=${base_dir%\"} ;;
    \'*\') base_dir=${base_dir#\'}; base_dir=${base_dir%\'} ;;
  esac
  if [ -n "$base_dir" ]; then
    case "$base_dir" in
      /*) data_path=$base_dir ;;
      *) data_path="$INSTALL/$base_dir" ;;
    esac
    if [ -d "$data_path" ] && [ ! -L "$data_path" ]; then
      install_real=$(cd "$INSTALL" && pwd -P)
      data_real=$(cd "$data_path" && pwd -P)
      case "$data_real" in
        "$install_real"/_internal|"$install_real"/_internal/*) ;;
        "$install_real"/*)
          userdata_rel=${data_real#"$install_real"/}
          if [ -e "$userdata_stash" ]; then
            fail "A previous update left the data folder beside Yaver. Move that folder back before updating again."
          fi
          mv "$data_real" "$userdata_stash"
          ;;
      esac
    fi
  fi
fi

unpark_userdata() {
  if [ -z "${userdata_rel:-}" ] || [ ! -d "${userdata_stash:-}" ]; then
    return 0
  fi
  dest="$INSTALL/$userdata_rel"
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  mv "$userdata_stash" "$dest"
}

repark_userdata() {
  if [ -z "${userdata_rel:-}" ] || [ -d "${userdata_stash:-}" ]; then
    return 0
  fi
  src="$INSTALL/$userdata_rel"
  if [ -d "$src" ] && [ ! -L "$src" ]; then
    mv "$src" "$userdata_stash"
  fi
}

restore_previous() {
  if [ -n "${new_pid:-}" ]; then
    kill "$new_pid" 2>/dev/null || true
    sleep 1
    kill -9 "$new_pid" 2>/dev/null || true
    new_pid=""
  fi
  repark_userdata || true
  broken="$parent/$leaf.broken"
  rm -rf "$broken"
  if [ -d "$INSTALL" ]; then
    mv "$INSTALL" "$broken" || true
  fi
  if [ -d "$previous" ] && [ ! -d "$INSTALL" ]; then
    mv "$previous" "$INSTALL" || true
  fi
  unpark_userdata || true
}

port_value=${PORT:-0}
if [ "$port_value" -gt 0 ]; then
  waited=0
  while [ "$waited" -lt 30 ]; do
    if ! port_open "$PROBE" "$port_value"; then
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  # An old listener still on this port would look like the new copy opened.
  if port_open "$PROBE" "$port_value"; then
    unpark_userdata || true
    fail "The dashboard port is still open."
  fi
fi

start_argv() {
  # This script is the session leader. A child left in that session gets
  # SIGHUP when the script exits, and the new Yaver dies with it.
  if [ -n "${LOG:-}" ]; then
    if [ -n "${ARGV1:-}" ]; then
      setsid "$ARGV0" "$ARGV1" >>"$LOG" 2>&1 </dev/null &
    else
      setsid "$ARGV0" >>"$LOG" 2>&1 </dev/null &
    fi
  else
    if [ -n "${ARGV1:-}" ]; then
      setsid "$ARGV0" "$ARGV1" >/dev/null 2>&1 </dev/null &
    else
      setsid "$ARGV0" >/dev/null 2>&1 </dev/null &
    fi
  fi
  new_pid=$!
}

rm -rf "$previous"
if ! mv "$INSTALL" "$previous"; then
  unpark_userdata || true
  fail "Could not move the install folder."
fi
if ! cp -a "$STAGING" "$INSTALL"; then
  restore_previous
  fail "Could not copy the new files."
fi
if [ -n "$env_copy" ] && [ -f "$env_copy" ]; then
  cp -p "$env_copy" "$INSTALL/.env"
  rm -f "$env_copy"
  env_copy=""
fi
if [ -n "$userdata_rel" ] && [ -d "$userdata_stash" ]; then
  unpark_userdata
fi

if [ -z "${ARGV0:-}" ]; then
  restore_previous
  fail "The update plan has no start command."
fi
start_argv
health=${HEALTH_SECONDS:-45}
if [ "$port_value" -gt 0 ] && [ "$health" -gt 0 ]; then
  waited=0
  opened=0
  while [ "$waited" -lt "$health" ]; do
    if port_open "$PROBE" "$port_value"; then
      opened=1
      break
    fi
    sleep 1
    waited=$((waited + 1))
  done
  if [ "$opened" -ne 1 ]; then
    restore_previous
    log_line "starting restored ${ARGV0}"
    start_argv
    fail "The new Yaver did not open. The previous copy was restored."
  fi
fi
log_line "started ${VERSION:-}"
write_result "{\"ok\": true, \"version\": \"${VERSION:-}\", \"error\": \"\"}"
exit 0
