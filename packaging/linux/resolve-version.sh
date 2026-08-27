#!/usr/bin/env bash
# SemVer product version for Linux dist builds. Same rules as
# packaging/windows/resolve-version.ps1.
set -euo pipefail

ROOT="${1:-}"
if [[ -z "$ROOT" ]]; then
  ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
fi
PRODUCT_NAME="${2:-virtual_developer-linux-x64}"
DIST_SUFFIX="${3:-}"

VERSION_FILE="$ROOT/VERSION"
if [[ ! -f "$VERSION_FILE" ]]; then
  echo "VERSION file not found: $VERSION_FILE" >&2
  exit 1
fi
base="$(tr -d '[:space:]' <"$VERSION_FILE")"
if [[ ! "$base" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  echo "VERSION must be MAJOR.MINOR.PATCH (got: $base)" >&2
  exit 1
fi

sha="${GITHUB_SHA:-}"
if [[ -n "$sha" ]]; then
  sha="${sha:0:7}"
else
  sha="$(git -C "$ROOT" rev-parse --short=7 HEAD 2>/dev/null || echo unknown)"
fi
run="${GITHUB_RUN_NUMBER:-0}"
date="$(date -u +%Y%m%d)"

channel="dev"
ref="${GITHUB_REF_NAME:-}"
head="${GITHUB_HEAD_REF:-}"
if [[ "${GITHUB_REF_TYPE:-}" == "tag" ]]; then
  channel="release"
elif [[ "${GITHUB_EVENT_NAME:-}" == "pull_request" && -n "$head" ]]; then
  ref="$head"
fi
if [[ "$channel" != "release" ]]; then
  if [[ "$ref" == "main" || "$ref" == "master" ]]; then
    channel="main"
  elif [[ "$ref" == "develop" ]]; then
    channel="develop"
  fi
fi

version="$base"
if [[ "${GITHUB_REF_TYPE:-}" == "tag" ]]; then
  tag="${GITHUB_REF_NAME:-$base}"
  version="${tag#v}"
  channel="release"
elif [[ "$channel" == "main" ]]; then
  version="${base}+g${sha}"
elif [[ "$channel" == "develop" ]]; then
  version="${base}-dev.${date}.${run}+g${sha}"
else
  version="${base}-dev.${sha}"
fi
if [[ -n "$DIST_SUFFIX" && "$channel" != "release" ]]; then
  version="${version}-${DIST_SUFFIX}"
fi

# Artifact names cannot contain +
version_safe="${version//+/-}"
dist_name="${PRODUCT_NAME}-${version_safe}"

echo "version=$version"
echo "version_safe=$version_safe"
echo "dist_name=$dist_name"
echo "channel=$channel"
echo "base_version=$base"

if [[ -n "${GITHUB_OUTPUT:-}" ]]; then
  {
    echo "version=$version"
    echo "version_safe=$version_safe"
    echo "dist_name=$dist_name"
    echo "channel=$channel"
    echo "base_version=$base"
  } >>"$GITHUB_OUTPUT"
fi
