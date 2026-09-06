#!/usr/bin/env bash
set -euo pipefail
export HOME=/root
src="$1"
dest="$2"
python3 -c "from pathlib import Path; import sys; Path(sys.argv[2]).write_bytes(Path(sys.argv[1]).read_bytes().replace(b'\r\n', b'\n'))" "$src" "$dest"
shift 2
exec python3 "$dest" "$@"
