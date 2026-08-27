#!/usr/bin/env bash
# Build a Linux x64 offline zip: app + web/dist + vendor (OpenCode, Codex, wheels).
# Intended for ubuntu-latest in GitHub Actions (or a local Linux box).
set -euo pipefail

ROOT="${VD_REPO_ROOT:-}"
OUT_DIR="${VD_OUT_DIR:-}"
DIST_NAME="${VD_DIST_NAME:-virtual_developer-linux-x64}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [[ -z "$ROOT" ]]; then
  ROOT="$(cd "$HERE/../.." && pwd)"
fi
if [[ -z "$OUT_DIR" ]]; then
  OUT_DIR="$ROOT/dist"
fi

read_versions() {
  local key="$1"
  local line
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%%#*}"
    line="$(echo "$line" | tr -d '\r')"
    [[ -z "${line// }" ]] && continue
    if [[ "$line" == "$key="* ]]; then
      printf '%s\n' "${line#*=}"
      return 0
    fi
  done <"$ROOT/packaging/windows/versions.env"
  return 1
}

OPENCODE_VERSION="$(read_versions OPENCODE_VERSION)"
OPENCODE_LINUX_ASSET="$(read_versions OPENCODE_LINUX_ASSET || true)"
OPENCODE_LINUX_ASSET="${OPENCODE_LINUX_ASSET:-opencode-linux-x64.tar.gz}"
GLAB_VERSION="$(read_versions GLAB_VERSION)"
CODEX_VERSION="$(read_versions CODEX_VERSION)"
CODEX_LINUX_ASSET="$(read_versions CODEX_LINUX_ASSET || true)"
CODEX_LINUX_ASSET="${CODEX_LINUX_ASSET:-codex-x86_64-unknown-linux-musl.tar.gz}"
PYTHON_MIN_VERSION="$(read_versions PYTHON_MIN_VERSION || true)"
PYTHON_MIN_VERSION="${PYTHON_MIN_VERSION:-3.10}"
WHEEL_VERS="$(read_versions PYTHON_WHEEL_VERSIONS || true)"
WHEEL_VERS="${WHEEL_VERS:-3.10,3.11,3.12,3.13}"
IFS=',' read -r -a WHEEL_LIST <<<"$WHEEL_VERS"

if [[ -z "$OPENCODE_VERSION" || -z "$GLAB_VERSION" || -z "$CODEX_VERSION" ]]; then
  echo "Missing OPENCODE_VERSION / GLAB_VERSION / CODEX_VERSION in versions.env" >&2
  exit 1
fi

mkdir -p "$OUT_DIR"
STAGE="$OUT_DIR/stage"
PAYLOAD="$STAGE/$DIST_NAME"
rm -rf "$STAGE"
mkdir -p "$PAYLOAD"

echo "========================================"
echo "  Building Linux distribution"
echo "========================================"
echo "Repo root : $ROOT"
echo "Payload   : $PAYLOAD"
echo "OpenCode  : $OPENCODE_VERSION ($OPENCODE_LINUX_ASSET)"
echo "Codex     : $CODEX_VERSION ($CODEX_LINUX_ASSET)"
echo "glab      : $GLAB_VERSION"
echo "Wheels    : ${WHEEL_LIST[*]}"
echo

echo "Step 1: Staging application files..."
copy_items=(
  cli.py requirements.txt .env.example VERSION README.md AGENTS.md
  commitMsgFormat.md pytest.ini
  install.sh install-dashboard.sh install-backends.sh install-codex.sh
  start.sh start-backend.sh start-frontend.sh start-opencode.sh
  start-opencode-serve.sh stop.sh
  src agent sample_project packaging
)
for item in "${copy_items[@]}"; do
  src="$ROOT/$item"
  if [[ ! -e "$src" ]]; then
    echo "  skip missing: $item"
    continue
  fi
  cp -a "$src" "$PAYLOAD/$item"
  echo "  + $item"
done
chmod +x "$PAYLOAD"/install*.sh "$PAYLOAD"/start*.sh "$PAYLOAD"/stop.sh \
  "$PAYLOAD/packaging/linux/"*.sh 2>/dev/null || true

echo
echo "Step 1b: Building ops dashboard frontend (web/)..."
if [[ ! -f "$ROOT/web/package.json" ]]; then
  echo "web/package.json missing" >&2
  exit 1
fi
(
  cd "$ROOT/web"
  if [[ -f package-lock.json ]]; then
    npm ci --no-fund --no-audit || npm install --no-fund --no-audit
  else
    npm install --no-fund --no-audit
  fi
  npm run build
)
if [[ ! -f "$ROOT/web/dist/index.html" ]]; then
  echo "web/dist/index.html missing after build" >&2
  exit 1
fi
mkdir -p "$PAYLOAD/web/dist"
cp -a "$ROOT/web/dist/." "$PAYLOAD/web/dist/"
cp -f "$ROOT/web/package.json" "$PAYLOAD/web/package.json"
if [[ -d "$PAYLOAD/web/node_modules" ]]; then
  echo "FAIL: web/node_modules must not be staged" >&2
  exit 1
fi
echo "  + web/dist"

VENDOR="$PAYLOAD/vendor"
DL="$VENDOR/_downloads"
mkdir -p "$DL" "$VENDOR/bin"

download() {
  local url="$1"
  local out="$2"
  echo "  Downloading $url"
  curl -fL --retry 3 --retry-delay 2 -o "$out" "$url"
  local bytes
  bytes="$(wc -c <"$out" | tr -d ' ')"
  echo "  OK ($bytes bytes)"
}

echo
echo "Step 2: Fetching OpenCode CLI v$OPENCODE_VERSION..."
OC_TAR="$DL/$OPENCODE_LINUX_ASSET"
download \
  "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/${OPENCODE_LINUX_ASSET}" \
  "$OC_TAR"
OC_EXT="$DL/opencode-extract"
rm -rf "$OC_EXT"
mkdir -p "$OC_EXT"
tar -xzf "$OC_TAR" -C "$OC_EXT"
OC_BIN="$(find "$OC_EXT" -type f -name opencode -print -quit)"
if [[ -z "$OC_BIN" || ! -f "$OC_BIN" ]]; then
  echo "opencode binary not found in $OPENCODE_LINUX_ASSET" >&2
  exit 1
fi
chmod +x "$OC_BIN"
cp -f "$OC_BIN" "$VENDOR/bin/opencode"
echo "  OpenCode SHA256: $(sha256sum "$VENDOR/bin/opencode" | awk '{print $1}')"

echo
echo "Step 3: Fetching glab v$GLAB_VERSION..."
GLAB_TAR="$DL/glab_${GLAB_VERSION}_linux_amd64.tar.gz"
download \
  "https://gitlab.com/api/v4/projects/gitlab-org%2Fcli/packages/generic/glab/${GLAB_VERSION}/glab_${GLAB_VERSION}_linux_amd64.tar.gz" \
  "$GLAB_TAR" \
  || download \
    "https://gitlab.com/gitlab-org/cli/-/releases/v${GLAB_VERSION}/downloads/glab_${GLAB_VERSION}_linux_amd64.tar.gz" \
    "$GLAB_TAR"
GLAB_EXT="$DL/glab-extract"
rm -rf "$GLAB_EXT"
mkdir -p "$GLAB_EXT"
tar -xzf "$GLAB_TAR" -C "$GLAB_EXT"
GLAB_BIN="$(find "$GLAB_EXT" -type f -name glab -print -quit)"
if [[ -z "$GLAB_BIN" || ! -f "$GLAB_BIN" ]]; then
  echo "glab binary not found" >&2
  exit 1
fi
chmod +x "$GLAB_BIN"
cp -f "$GLAB_BIN" "$VENDOR/bin/glab"

echo
echo "Step 3b: Fetching Codex CLI v$CODEX_VERSION..."
CODEX_TAR="$DL/$CODEX_LINUX_ASSET"
download \
  "https://github.com/openai/codex/releases/download/rust-v${CODEX_VERSION}/${CODEX_LINUX_ASSET}" \
  "$CODEX_TAR"
cp -f "$CODEX_TAR" "$VENDOR/$CODEX_LINUX_ASSET"
CODEX_EXT="$DL/codex-extract"
rm -rf "$CODEX_EXT"
mkdir -p "$CODEX_EXT"
tar -xzf "$CODEX_TAR" -C "$CODEX_EXT"
CODEX_BIN="$(find "$CODEX_EXT" -type f \( -name 'codex' -o -name 'codex-*linux*' \) -print -quit)"
if [[ -z "$CODEX_BIN" || ! -f "$CODEX_BIN" ]]; then
  echo "codex binary not found in $CODEX_LINUX_ASSET" >&2
  exit 1
fi
chmod +x "$CODEX_BIN"
cp -f "$CODEX_BIN" "$VENDOR/bin/codex"
echo "  Codex SHA256: $(sha256sum "$VENDOR/bin/codex" | awk '{print $1}')"

echo
echo "Step 4: Building vendor/opencode-home.zip..."
OC_HOME="$DL/opencode-home"
rm -rf "$OC_HOME"
mkdir -p "$OC_HOME/bin"
cp -f "$VENDOR/bin/opencode" "$OC_HOME/bin/opencode"
cp -f "$VENDOR/bin/glab" "$OC_HOME/bin/glab"
cp -f "$HERE/opencode.json" "$OC_HOME/opencode.json"
python3 - "$OC_HOME" "$VENDOR/opencode-home.zip" <<'PY'
import pathlib, sys, zipfile
root = pathlib.Path(sys.argv[1])
dest = pathlib.Path(sys.argv[2])
with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
    for path in root.rglob("*"):
        if path.is_file():
            zf.write(path, path.relative_to(root).as_posix())
print("wrote", dest)
PY
echo "  + vendor/opencode-home.zip"

echo
echo "Step 5: Downloading Python wheels (manylinux x86_64)..."
WHEELS="$VENDOR/python-wheels"
mkdir -p "$WHEELS"
python3 -m pip download -r "$ROOT/requirements.txt" -d "$WHEELS" --prefer-binary
python3 -m pip download pip setuptools wheel -d "$WHEELS" --prefer-binary
for pv in "${WHEEL_LIST[@]}"; do
  pv="$(echo "$pv" | tr -d ' ')"
  [[ -n "$pv" ]] || continue
  tag="${pv//./}"
  echo "  pip download manylinux cp$tag (Python $pv)..."
  python3 -m pip download -r "$ROOT/requirements.txt" -d "$WHEELS" \
    --python-version "$pv" \
    --platform manylinux2014_x86_64 \
    --implementation cp \
    --abi "cp${tag}" \
    --only-binary=:all: \
    --prefer-binary \
    || echo "  WARNING: incomplete wheel set for Python $pv"
done
echo "SUPPORTED_PYTHON=${WHEEL_LIST[*]}" >"$VENDOR/SUPPORTED_PYTHON.txt"

PRODUCT_VERSION="${VD_PRODUCT_VERSION:-}"
if [[ -z "$PRODUCT_VERSION" ]]; then
  PRODUCT_VERSION="$(tr -d '[:space:]' <"$ROOT/VERSION")"
fi
cat >"$PAYLOAD/DIST_VERSION.txt" <<EOF
virtual_developer Linux offline distribution
ProductVersion=$PRODUCT_VERSION
DistName=$DIST_NAME
OpenCode=$OPENCODE_VERSION
OpenCodeAsset=$OPENCODE_LINUX_ASSET
Codex=$CODEX_VERSION
CodexAsset=$CODEX_LINUX_ASSET
glab=$GLAB_VERSION
PythonMin=$PYTHON_MIN_VERSION
PythonWheels=${WHEEL_VERS}
OpenCodeHome=vendor/opencode-home.zip
EOF
printf '%s\n' "$PRODUCT_VERSION" >"$PAYLOAD/VERSION"

echo
echo "Step 6: Zipping payload..."
mkdir -p "$OUT_DIR"
(
  cd "$STAGE"
  tar -czf "$OUT_DIR/${DIST_NAME}.tar.gz" "$DIST_NAME"
)
python3 - "$STAGE" "$DIST_NAME" "$OUT_DIR/${DIST_NAME}.zip" <<'PY'
import shutil, sys
stage, name, dest = sys.argv[1], sys.argv[2], sys.argv[3]
shutil.make_archive(dest[:-4], "zip", stage, name)
print("wrote", dest)
PY
echo
echo "[OK] $OUT_DIR/${DIST_NAME}.tar.gz"
echo "[OK] $OUT_DIR/${DIST_NAME}.zip"
echo "Payload: $PAYLOAD"
