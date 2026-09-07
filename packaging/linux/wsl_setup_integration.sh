#!/usr/bin/env bash
# Prepare /root/yaver-wsl-test from downloaded 0.2.3 zip + OpenCode linux tarball.
set -euo pipefail
export HOME=/root
export PATH="/root/.opencode/bin:${PATH}"
WORKDIR=/root/yaver-wsl-test
REPO=/mnt/c/Users/BERAT/virtual_developer
cd "$WORKDIR"

rm -rf app
mkdir -p app
python3 -c 'import zipfile; z=zipfile.ZipFile("yaver-linux-x64-0.2.3.zip"); z.extractall("app"); print("extracted", len(z.namelist()), "files")'

YAVER_BIN="$(find app -maxdepth 4 -type f -name yaver | head -1)"
if [[ -z "$YAVER_BIN" ]]; then
  echo "yaver binary missing after extract" >&2
  find app -maxdepth 3 -print | head
  exit 1
fi
chmod +x "$YAVER_BIN"
echo "yaver=$YAVER_BIN"

mkdir -p /tmp/oc-unpack /root/.opencode/bin /root/.opencode/agents /root/.opencode/skills
tar -xzf opencode-linux-x64.tar.gz -C /tmp/oc-unpack
OC_BIN="$(find /tmp/oc-unpack -type f -name opencode | head -1)"
cp -f "$OC_BIN" /root/.opencode/bin/opencode
chmod +x /root/.opencode/bin/opencode
/root/.opencode/bin/opencode --version

OCM="$REPO/opencoderman"
if [[ -d "$OCM/agents" ]]; then
  cp -f "$OCM/agents/"*.md /root/.opencode/agents/
  rm -rf /root/.opencode/skills
  mkdir -p /root/.opencode/skills
  cp -a "$OCM/skills/." /root/.opencode/skills/
  echo "skills=$(find /root/.opencode/skills -name SKILL.md | wc -l)"
fi
cp -f "$REPO/packaging/linux/opencode.integration.json" /root/.opencode/opencode.json

# Rewrite Windows .env for this WSL payload (paths + ports). Do not print secrets.
python3 - "$REPO/.env" "$WORKDIR/app/.env" <<'PY'
import sys
from pathlib import Path
src, dest = Path(sys.argv[1]), Path(sys.argv[2])
# dest may be inside a versioned folder
text = src.read_text(encoding="utf-8", errors="replace")
repl = {
    "DASHBOARD_HOST=0.0.0.0": "DASHBOARD_HOST=127.0.0.1",
    "DASHBOARD_PORT=8080": "DASHBOARD_PORT=18081",
    "OPENCODE_SERVE_URL=http://127.0.0.1:4096": "OPENCODE_SERVE_URL=http://127.0.0.1:14097",
    r"TEMP_DIR_BASE=C:\vd\t": "TEMP_DIR_BASE=/root/vd-t",
    r"YAVER_DATA_DIR=C:\vd\yaver": "YAVER_DATA_DIR=/root/vd-yaver",
}
for a, b in repl.items():
    text = text.replace(a, b)
dest.parent.mkdir(parents=True, exist_ok=True)
dest.write_text(text, encoding="utf-8")
print("wrote", dest, "bytes", dest.stat().st_size)
PY

# If zip extracted a nested folder, copy .env next to yaver
YAVER_DIR="$(dirname "$YAVER_BIN")"
if [[ "$YAVER_DIR" != "$WORKDIR/app" ]]; then
  cp -f "$WORKDIR/app/.env" "$YAVER_DIR/.env" 2>/dev/null || true
fi
# Prefer .env beside the binary
python3 - "$REPO/.env" "$YAVER_DIR/.env" <<'PY'
import sys
from pathlib import Path
src, dest = Path(sys.argv[1]), Path(sys.argv[2])
text = src.read_text(encoding="utf-8", errors="replace")
repl = {
    "DASHBOARD_HOST=0.0.0.0": "DASHBOARD_HOST=127.0.0.1",
    "DASHBOARD_PORT=8080": "DASHBOARD_PORT=18081",
    "OPENCODE_SERVE_URL=http://127.0.0.1:4096": "OPENCODE_SERVE_URL=http://127.0.0.1:14097",
    "TEMP_DIR_BASE=C:\\vd\\t": "TEMP_DIR_BASE=/root/vd-t",
    "YAVER_DATA_DIR=C:\\vd\\yaver": "YAVER_DATA_DIR=/root/vd-yaver",
}
for a, b in repl.items():
    text = text.replace(a, b)
dest.write_text(text, encoding="utf-8")
print("wrote", dest)
PY

mkdir -p /root/vd-t /root/vd-yaver
echo "YAVER_BIN=$YAVER_BIN" > /root/yaver-wsl-test/paths.env
echo "SETUP_OK"