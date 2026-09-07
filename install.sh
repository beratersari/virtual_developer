#!/usr/bin/env bash
# Convenience: dashboard then agent backends (same split as Windows).
# Prefer the individual scripts if you only need one half.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Virtual Developer — Linux install"
echo "  1) dashboard (Python .venv)"
echo "  2) backends  (OpenCode + Codex)"
echo

"$ROOT/packaging/linux/install-dashboard.sh"
echo
"$ROOT/packaging/linux/install-backends.sh"

echo
echo "Done. Edit .env, then:  ./start.sh"
echo "Or separately: ./start-backend.sh   ./start-frontend.sh"
echo
