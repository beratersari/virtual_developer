#!/usr/bin/env bash
set -euo pipefail
export HOME=/root
export DEFAULT_MODEL="${DEFAULT_MODEL:-opencode/hy3-free}"
export YAVER_BASE="${YAVER_BASE:-http://127.0.0.1:18081}"
export OPENCODE_BASE="${OPENCODE_BASE:-http://127.0.0.1:14097}"
SRC=/mnt/c/Users/BERAT/virtual_developer/packaging/linux/wsl_integration_probe.py
DEST=/root/yaver-wsl-test/wsl_integration_probe.py
python3 -c "from pathlib import Path; s=Path('$SRC').read_bytes().replace(b'\r\n', b'\n'); Path('$DEST').write_bytes(s)"
exec python3 "$DEST"
