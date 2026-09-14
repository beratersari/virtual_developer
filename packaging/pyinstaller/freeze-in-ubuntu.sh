#!/bin/bash
# Freeze Yaver inside an Ubuntu container so collected libs match that
# release's glibc. Invoked by .github/workflows/executables.yml.
#
# Required env: VD_PRODUCT_VERSION, VD_DIST_NAME, PYINSTALLER_VERSION
# Required mount: /opt/python.tar.gz  (python-build-standalone install_only)
# CWD: repo root
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
export PYTHONUTF8=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

rewrite_apt_old_releases() {
  if [ -f /etc/apt/sources.list ]; then
    sed -i \
      -e 's|https://archive.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
      -e 's|http://archive.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
      -e 's|https://security.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
      -e 's|http://security.ubuntu.com/ubuntu|http://old-releases.ubuntu.com/ubuntu|g' \
      /etc/apt/sources.list
  fi
}

if ! apt-get -o Acquire::Check-Valid-Until=false update; then
  rewrite_apt_old_releases
  apt-get -o Acquire::Check-Valid-Until=false update
fi

apt-get install -y --no-install-recommends \
  ca-certificates tar gzip binutils git \
  build-essential libffi-dev libssl-dev zlib1g-dev

git config --global --add safe.directory /src || true
git config --global --add safe.directory /src/opencoderman || true

if [ ! -f /opt/python.tar.gz ]; then
  echo "missing /opt/python.tar.gz (standalone CPython tarball)" >&2
  exit 1
fi

PYROOT=/opt/yaver-python
rm -rf "$PYROOT"
mkdir -p "$PYROOT"
tar -xzf /opt/python.tar.gz -C "$PYROOT" --strip-components=1
export PATH="$PYROOT/bin:$PATH"

python3 --version
python3 -m pip install --upgrade pip
python3 -m pip install -r requirements.txt
python3 -m pip install "pyinstaller==${PYINSTALLER_VERSION}"
python3 packaging/pyinstaller/build.py --clean --out-dir dist --dist-name "${VD_DIST_NAME}"

payload="dist/stage/${VD_DIST_NAME}"
if [ ! -x "${payload}/yaver" ]; then
  chmod +x "${payload}/yaver"
fi
"${payload}/yaver" --version
"${payload}/yaver" --help
chmod -R a+rX dist
