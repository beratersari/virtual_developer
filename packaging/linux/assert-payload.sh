#!/usr/bin/env bash
# Fast layout check for a staged Linux offline payload.
set -euo pipefail
p="${1:?payload dir}"
if [[ ! -d "$p" ]]; then
  echo "Payload dir missing: $p" >&2
  exit 1
fi
for rel in \
  install-dashboard.sh \
  install-backends.sh \
  install-codex.sh \
  start.sh \
  start-backend.sh \
  start-frontend.sh \
  start-opencode.sh \
  start-opencode-serve.sh \
  stop.sh \
  cli.py \
  src/daemon.py \
  web/dist/index.html \
  vendor/opencode-home.zip \
  vendor/bin/opencode \
  vendor/bin/glab \
  vendor/bin/codex \
  vendor/python-wheels \
  packaging/linux/opencode.json \
  packaging/windows/serve_frontend.py
do
  if [[ ! -e "$p/$rel" ]]; then
    echo "Missing required payload path: $rel" >&2
    exit 1
  fi
  echo "OK $rel"
done
if [[ -d "$p/web/node_modules" ]]; then
  echo "web/node_modules must not be in the offline zip" >&2
  exit 1
fi
if ! ls "$p/web/dist/assets"/index-*.js >/dev/null 2>&1; then
  echo "web/dist/assets/index-*.js missing" >&2
  exit 1
fi
if ! ls "$p"/vendor/codex-*.tar.gz >/dev/null 2>&1; then
  echo "vendor/codex-*.tar.gz missing" >&2
  exit 1
fi
echo "Payload layout OK"
