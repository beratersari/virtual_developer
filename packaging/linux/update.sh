#!/bin/sh
# Update Yaver in this folder. Run this file yourself.
# It stops yaver, then replaces the program files.
# .env and this file stay as they are. Start ./yaver when this script finishes.
set -eu

CR=$(printf '\r')

say() {
  printf '%s\n' "$1"
}

fail() {
  say "$1"
  exit 1
}

ROOT=$(CDPATH= cd -- "$(dirname "$0")" && pwd)
WORK=

cleanup() {
  if [ -n "${WORK:-}" ] && [ -d "$WORK" ]; then
    rm -rf "$WORK"
  fi
}
trap cleanup EXIT

version_id() {
  text=$1
  text=${text%%+*}
  text=${text%"$CR"}
  text=${text#"${text%%[![:space:]]*}"}
  text=${text%"${text##*[![:space:]]}"}
  printf '%s\n' "$text"
}

env_value() {
  key=$1
  file=$2
  found=
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%"$CR"}
    case "$line" in
      ''|\#*) continue ;;
    esac
    case "$line" in
      'export '*) line=${line#export } ;;
    esac
    case "$line" in
      "$key"=*) found=${line#*=} ;;
    esac
  done < "$file"
  found=${found#"${found%%[![:space:]]*}"}
  found=${found%"${found##*[![:space:]]}"}
  case "$found" in
    \"*\") found=${found#\"}; found=${found%\"} ;;
    \'*\') found=${found#\'}; found=${found%\'} ;;
  esac
  printf '%s\n' "$found"
}

json_string() {
  key=$1
  file=$2
  # First quoted value for this key. sed would keep the unmatched prefix,
  # so {"version": "1.2.3"} would become {1.2.3}.
  tr -d '\r\n' < "$file" | awk -v key="$key" '
    {
      needle = "\"" key "\""
      p = index($0, needle)
      if (p == 0) exit 0
      rest = substr($0, p + length(needle))
      colon = index(rest, ":")
      if (colon == 0) exit 0
      rest = substr(rest, colon + 1)
      q1 = index(rest, "\"")
      if (q1 == 0) exit 0
      rest = substr(rest, q1 + 1)
      q2 = index(rest, "\"")
      if (q2 == 0) exit 0
      printf "%s\n", substr(rest, 1, q2 - 1)
    }
  '
}

restore_moved() {
  list=$1
  [ -f "$list" ] || return 0
  awk '{ lines[NR] = $0 } END { for (i = NR; i >= 1; i--) print lines[i] }' "$list" |
    while IFS= read -r name; do
      [ -n "$name" ] || continue
      dest="$ROOT/$name"
      hold="$WORK/hold/$name"
      rm -rf "$dest"
      if [ -e "$hold" ] || [ -L "$hold" ]; then
        mv "$hold" "$dest" || true
      fi
    done
}

output_path() {
  if command -v cygpath >/dev/null 2>&1; then
    cygpath -m "$1"
    return
  fi
  printf '%s\n' "$1"
}

fetch_url() {
  url=$1
  dest=$2
  message=$3
  status=0
  out=$(output_path "$dest")
  code=$(curl --silent --show-error --connect-timeout 15 --max-time 600 --max-redirs 0 --noproxy '*' -o "$out" -w '%{http_code}' "$url") || status=$?
  if [ "$status" -ne 0 ] || [ "$code" != "200" ]; then
    fail "$message"
  fi
}

name_is_safe() {
  norm=$(printf '%s' "$1" | tr '\\' '/')
  case "$norm" in
    /*|[A-Za-z]:*) return 1 ;;
  esac
  case "$norm" in
    ..|../*|*/..|*/../*) return 1 ;;
  esac
  return 0
}

extract_zip() {
  zip=$1
  dest=$2
  if command -v unzip >/dev/null 2>&1; then
    unzip -Z1 "$zip" > "$WORK/names.txt" || fail "Could not open the package."
    while IFS= read -r name || [ -n "$name" ]; do
      name=${name%"$CR"}
      [ -n "$name" ] || continue
      name_is_safe "$name" || fail "The package contains an unsafe path."
    done < "$WORK/names.txt"
    unzip -q -o "$zip" -d "$dest" || fail "Could not open the package."
    return
  fi
  if ! command -v python3 >/dev/null 2>&1; then
    fail "unzip is required."
  fi
  status=0
  python3 -c '
import sys, zipfile
archive = zipfile.ZipFile(sys.argv[1])
dest = sys.argv[2]
for info in archive.infolist():
    name = info.filename.replace("\\", "/")
    if name.startswith("/") or (len(name) > 1 and name[1] == ":"):
        raise SystemExit(2)
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise SystemExit(2)
archive.extractall(dest)
' "$zip" "$dest" || status=$?
  if [ "$status" -eq 2 ]; then
    fail "The package contains an unsafe path."
  fi
  if [ "$status" -ne 0 ]; then
    fail "Could not open the package."
  fi
}

if [ -z "$ROOT" ] || [ "$ROOT" = "/" ]; then
  fail "Refusing to update a drive root."
fi
if [ -e "$ROOT/.git" ]; then
  fail "This folder is a git checkout. Update replaces an installed copy."
fi
if [ ! -f "$ROOT/.env" ]; then
  fail ".env is missing. Copy .env.example to .env and set RELEASE_HOST and RELEASE_PORT."
fi
if [ ! -f "$ROOT/yaver" ] || [ ! -d "$ROOT/_internal" ]; then
  fail "This folder is not a Yaver executable install."
fi

rel_host=$(env_value RELEASE_HOST "$ROOT/.env")
rel_port=$(env_value RELEASE_PORT "$ROOT/.env")
case "$rel_host" in
  [A-Za-z0-9]*) ;;
  *) fail "Set RELEASE_HOST in .env to an IP or a hostname." ;;
esac
case "$rel_host" in
  *[!A-Za-z0-9.-]*) fail "Set RELEASE_HOST in .env to an IP or a hostname." ;;
esac
case "$rel_port" in
  *[!0-9]*|0*) fail "Set RELEASE_PORT in .env to a port from 1 to 65535." ;;
esac
if [ "$rel_port" -gt 65535 ]; then
  fail "Set RELEASE_PORT in .env to a port from 1 to 65535."
fi

if [ -n "${YAVER_UPDATE_PLATFORM:-}" ]; then
  plat=$YAVER_UPDATE_PLATFORM
else
  if [ ! -f /etc/os-release ]; then
    fail "Updates are published for Ubuntu 18.04, 20.04, 22.04, and 24.04."
  fi
  # shellcheck disable=SC1091
  . /etc/os-release
  case "${ID:-}" in
    ubuntu) ;;
    *) fail "Updates are published for Ubuntu 18.04, 20.04, 22.04, and 24.04." ;;
  esac
  ver=${VERSION_ID:-}
  case "$ver" in
    18.04*|20.04*|22.04*|24.04*) ;;
    *) fail "Ubuntu ${ver:-unknown} does not have a published package." ;;
  esac
  plat=$(printf '%s' "$ver" | cut -d. -f1,2)
  plat="ubuntu-${plat}"
fi
case "$plat" in
  ubuntu-18.04|ubuntu-20.04|ubuntu-22.04|ubuntu-24.04) ;;
  *) fail "This script updates an Ubuntu install." ;;
esac

say "Release server ${rel_host}:${rel_port}"
if ! command -v curl >/dev/null 2>&1; then
  fail "curl is required."
fi
if ! command -v awk >/dev/null 2>&1; then
  fail "awk is required."
fi
WORK=$(mktemp -d "${TMPDIR:-/tmp}/yaver-update.XXXXXX")
meta="$WORK/latest.json"
zip="$WORK/package.zip"
stage="$WORK/stage"
mkdir -p "$stage"
fetch_url "http://${rel_host}:${rel_port}/api/latest?platform=${plat}" "$meta" "Could not reach the release server."
version=$(json_string version "$meta")
sha=$(json_string sha256 "$meta" | tr 'A-F' 'a-f')
case "$version" in
  *[!0-9A-Za-z.+-]*) fail "The release server returned an unexpected version." ;;
esac
case "$version" in
  [0-9A-Za-z]*) ;;
  *) fail "The release server returned an unexpected version." ;;
esac
if [ "${#version}" -gt 64 ]; then
  fail "The release server returned an unexpected version."
fi
case "$sha" in
  *[!0-9a-f]*) fail "The release server returned an unexpected checksum." ;;
esac
if [ "${#sha}" -ne 64 ]; then
  fail "The release server returned an unexpected checksum."
fi
if [ -f "$ROOT/VERSION" ]; then
  local_id=$(version_id "$(tr -d '\r' < "$ROOT/VERSION")")
  remote_id=$(version_id "$version")
  if [ -n "$local_id" ] && [ "$local_id" = "$remote_id" ]; then
    say "This install is already $version."
    exit 0
  fi
fi
say "Downloading $version."
fetch_url "http://${rel_host}:${rel_port}/download/${plat}" "$zip" "Could not download the package."
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$zip" | awk '{print $1}' | tr 'A-F' 'a-f')
elif command -v shasum >/dev/null 2>&1; then
  actual=$(shasum -a 256 "$zip" | awk '{print $1}' | tr 'A-F' 'a-f')
else
  fail "sha256sum is required."
fi
if [ "$actual" != "$sha" ]; then
  fail "The package checksum does not match."
fi
say "Checking the package."
extract_zip "$zip" "$stage"
set -- "$stage"/*
if [ -d "$1" ] && [ ! -e "$stage/yaver" ]; then
  inner=$1
  if [ -f "$inner/yaver" ] && [ -d "$inner/_internal" ]; then
    stage=$inner
  fi
fi
if [ ! -f "$stage/yaver" ] || [ ! -d "$stage/_internal" ]; then
  fail "The published package is not a Yaver executable."
fi

say "Stopping Yaver."
if [ -d /proc ]; then
  for proc in /proc/[0-9]*; do
    pid=${proc##*/}
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in
      "$ROOT/yaver"|"$ROOT/yaver (deleted)")
        kill "$pid" 2>/dev/null || true
        say "Stopped yaver pid=$pid"
        ;;
    esac
  done
  sleep 1
  for proc in /proc/[0-9]*; do
    pid=${proc##*/}
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in
      "$ROOT/yaver"|"$ROOT/yaver (deleted)")
        kill -9 "$pid" 2>/dev/null || true
        ;;
    esac
  done
  for proc in /proc/[0-9]*; do
    exe=$(readlink "$proc/exe" 2>/dev/null || true)
    case "$exe" in
      "$ROOT/yaver"|"$ROOT/yaver (deleted)")
        fail "Yaver is still running. Close it and run update.sh again."
        ;;
    esac
  done
fi

say "Replacing the files."
mkdir -p "$WORK/hold"
: > "$WORK/moved.txt"
set +e
for child in "$stage"/* "$stage"/.[!.]* "$stage"/..?*; do
  if [ ! -e "$child" ] && [ ! -L "$child" ]; then
    continue
  fi
  name=$(basename "$child")
  case "$name" in
    *[!A-Za-z0-9._-]*)
      say "skipped $name"
      continue
      ;;
  esac
  dest="$ROOT/$name"
  case "$name" in
    .env)
      if [ -f "$dest" ]; then
        say "left .env in place"
      else
        cp -a "$child" "$dest" || {
          restore_moved "$WORK/moved.txt"
          fail "Could not copy .env."
        }
        say "copied .env"
      fi
      continue
      ;;
    update.sh)
      say "left update.sh in place"
      continue
      ;;
  esac
  hold="$WORK/hold/$name"
  rm -rf "$hold"
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    if ! mv "$dest" "$hold"; then
      restore_moved "$WORK/moved.txt"
      fail "Could not move $name aside. Close Yaver and run update.sh again."
    fi
    printf '%s\n' "$name" >> "$WORK/moved.txt"
  fi
  if ! cp -a "$child" "$dest"; then
    restore_moved "$WORK/moved.txt"
    fail "Could not copy $name."
  fi
  case "$name" in
    yaver|install-agents.sh) chmod 755 "$dest" || true ;;
  esac
  say "copied $name"
done
status=$?
set -e
if [ "$status" -ne 0 ]; then
  exit "$status"
fi
say "Updated to ${version}. Start ./yaver when you want."
exit 0
