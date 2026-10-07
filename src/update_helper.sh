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

say() {
  printf '%s\n' "$1"
}

write_result() {
  if [ -z "${RESULT:-}" ]; then
    return
  fi
  printf '%s\n' "$1" > "$RESULT"
}

fail() {
  say "$1"
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
say "helper install=$INSTALL staging=$STAGING layout=${LAYOUT:-} port=${PORT:-0} probe=$PROBE plan_pid=${PID:-0}"

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
# Park only after the port check. An earlier move pulls a live data
# folder out from under a Yaver that is still running.
userdata_rel=""
userdata_stash="$parent/$leaf.userdata"

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

move_one() {
  src=$1
  dest=$2
  if [ -e "$src" ] || [ -L "$src" ]; then
    mv "$src" "$dest" || return 1
  fi
}

undo_moves() {
  if [ ! -d "$previous" ]; then
    return 0
  fi
  for child in "$previous"/* "$previous"/.[!.]* "$previous"/..?*; do
    if [ ! -e "$child" ] && [ ! -L "$child" ]; then
      continue
    fi
    name=$(basename "$child")
    if [ -e "$INSTALL/$name" ] || [ -L "$INSTALL/$name" ]; then
      continue
    fi
    move_one "$child" "$INSTALL/$name" || true
  done
}

restore_previous() {
  # The install directory stays. A console may be using it as its cwd.
  repark_userdata || true
  broken="$parent/$leaf.broken"
  mkdir -p "$broken"
  for child in "$INSTALL"/* "$INSTALL"/.[!.]* "$INSTALL"/..?*; do
    if [ ! -e "$child" ] && [ ! -L "$child" ]; then
      continue
    fi
    name=$(basename "$child")
    if [ "$name" = ".env" ]; then
      continue
    fi
    move_one "$child" "$broken/$name" || true
  done
  if [ -d "$previous" ]; then
    for child in "$previous"/* "$previous"/.[!.]* "$previous"/..?*; do
      if [ ! -e "$child" ] && [ ! -L "$child" ]; then
        continue
      fi
      name=$(basename "$child")
      if [ -e "$INSTALL/$name" ] || [ -L "$INSTALL/$name" ]; then
        continue
      fi
      move_one "$child" "$INSTALL/$name" || true
    done
  fi
  unpark_userdata || true
}

copy_staging() {
  # Leave an existing .env alone. Copy one from the package only when missing.
  for child in "$STAGING"/* "$STAGING"/.[!.]* "$STAGING"/..?*; do
    if [ ! -e "$child" ] && [ ! -L "$child" ]; then
      continue
    fi
    name=$(basename "$child")
    if [ "$name" = ".env" ] && [ -f "$INSTALL/.env" ]; then
      say "package env file skipped"
      continue
    fi
    cp -a "$child" "$INSTALL/" || return 1
    say "copied $name"
  done
}

# An existing .env stays where it is, so this read does not write the file.
park_userdata() {
  if [ ! -f "$INSTALL/.env" ]; then
    say "env file absent"
    return 0
  fi
  say "env file present"
  base_dir=$(grep -E '^YAVER_BASE_DIR=' "$INSTALL/.env" | tail -n 1 | cut -d= -f2- | tr -d '\r' || true)
  base_dir=${base_dir#"${base_dir%%[![:space:]]*}"}
  base_dir=${base_dir%"${base_dir##*[![:space:]]}"}
  case "$base_dir" in
    \"*\") base_dir=${base_dir#\"}; base_dir=${base_dir%\"} ;;
    \'*\') base_dir=${base_dir#\'}; base_dir=${base_dir%\'} ;;
  esac
  if [ -z "$base_dir" ]; then
    return 0
  fi
  case "$base_dir" in
    /*) data_path=$base_dir ;;
    *) data_path="$INSTALL/$base_dir" ;;
  esac
  if [ ! -d "$data_path" ] || [ -L "$data_path" ]; then
    return 0
  fi
  install_real=$(cd "$INSTALL" && pwd -P)
  data_real=$(cd "$data_path" && pwd -P)
  case "$data_real" in
    "$install_real"/_internal|"$install_real"/_internal/*) return 0 ;;
    "$install_real"/*) ;;
    *) return 0 ;;
  esac
  userdata_rel=${data_real#"$install_real"/}
  if [ -e "$userdata_stash" ]; then
    fail "A previous update left the data folder beside Yaver. Move that folder back before updating again."
  fi
  mv "$data_real" "$userdata_stash"
  say "parked data folder $userdata_rel"
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
    fail "The dashboard port is still open."
  fi
fi
say "dashboard port ${port_value} is closed"
park_userdata
# Covers rm/mkdir below. mv and cp failures unpark themselves too.
trap 'unpark_userdata || true' EXIT

rm -rf "$previous" || fail "Could not remove the previous copy."
mkdir -p "$previous" || fail "Could not prepare the previous copy."
say "previous=$previous"
for child in "$INSTALL"/* "$INSTALL"/.[!.]* "$INSTALL"/..?*; do
  if [ ! -e "$child" ] && [ ! -L "$child" ]; then
    continue
  fi
  name=$(basename "$child")
  if [ "$name" = ".env" ]; then
    say "env file left in place"
    continue
  fi
  if ! mv "$child" "$previous/$name"; then
    say "restoring previous files"
    undo_moves
    unpark_userdata || true
    fail "Could not move the install folder."
  fi
  say "moved $name"
done
if ! copy_staging; then
  say "restoring previous files"
  restore_previous
  fail "Could not copy the new files."
fi
say "frozen swap finished"
if [ -n "$userdata_rel" ] && [ -d "$userdata_stash" ]; then
  unpark_userdata
  say "restored data folder $userdata_rel"
fi
trap - EXIT

log_line "updated ${VERSION:-}"
write_result "{\"ok\": true, \"version\": \"${VERSION:-}\", \"error\": \"\"}"
echo "Updated to ${VERSION:-}. Start Yaver when you want."
exit 0
