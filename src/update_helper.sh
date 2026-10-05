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

rm -rf "$previous"
mv "$INSTALL" "$previous"
cp -a "$STAGING" "$INSTALL"
if [ -n "$env_copy" ] && [ -f "$env_copy" ]; then
  cp -p "$env_copy" "$INSTALL/.env"
  rm -f "$env_copy"
fi
if [ -n "$userdata_rel" ] && [ -d "$userdata_stash" ]; then
  dest="$INSTALL/$userdata_rel"
  mkdir -p "$(dirname "$dest")"
  rm -rf "$dest"
  mv "$userdata_stash" "$dest"
fi

# This script is the session leader. A child left in that session gets
# SIGHUP when the script exits, and the new Yaver dies with it.
if [ -n "${ARGV1:-}" ]; then
  setsid "$ARGV0" "$ARGV1" >/dev/null 2>&1 </dev/null &
else
  setsid "$ARGV0" >/dev/null 2>&1 </dev/null &
fi
log_line "started ${VERSION:-}"
write_result "{\"ok\": true, \"version\": \"${VERSION:-}\", \"error\": \"\"}"
exit 0
