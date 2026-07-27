#!/usr/bin/env bash
#
# Installer for the SIP intercom on Raspberry Pi OS (Debian bookworm, 64-bit).
# Primary targets: Raspberry Pi 4 and Pi 5. Also supported: Pi Zero 2 W.
#
# What it does:
#   1. Installs OS build + audio dependencies.
#   2. Builds pjproject and its PJSUA2 Python bindings (the SIP engine) into a
#      dedicated virtualenv.
#   3. Installs this app to /opt/sip-intercom with a venv (Flask, gpiozero).
#   4. Creates /etc/sip-intercom/config.json (if missing).
#   5. Installs and enables the systemd service.
#
# Re-runnable. Run as root:  sudo ./setup/install.sh
#
set -euo pipefail

APP_DIR=/opt/sip-intercom
CFG_DIR=/etc/sip-intercom
VENV="$APP_DIR/venv"
PJ_VERSION="2.14.1"
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"
BUILD_DIR=/usr/local/src

if [[ $EUID -ne 0 ]]; then
  echo "Please run as root (sudo)." >&2
  exit 1
fi

# Best-effort board detection (informational).
BOARD="unknown Raspberry Pi"
if [[ -r /proc/device-tree/model ]]; then
  BOARD="$(tr -d '\0' < /proc/device-tree/model)"
fi
echo "==> Detected board: $BOARD"
case "$BOARD" in
  *"Zero 2"*)
    echo "    Note: on the Zero 2 W the PJSUA2 build takes a while (limited RAM/CPU)."
    ;;
esac

echo "==> Installing OS dependencies…"
apt-get update
apt-get install -y --no-install-recommends \
  build-essential git pkg-config swig python3 python3-dev python3-venv \
  libasound2-dev libssl-dev libopus-dev alsa-utils curl ca-certificates

echo "==> Creating app directory $APP_DIR"
mkdir -p "$APP_DIR" "$CFG_DIR" "$BUILD_DIR"

echo "==> Copying application files"
cp -r "$SRC_DIR/sipintercom" "$SRC_DIR/web" "$SRC_DIR/requirements.txt" "$APP_DIR/"

echo "==> Creating virtualenv"
python3 -m venv "$VENV"
# shellcheck disable=SC1091
source "$VENV/bin/activate"
pip install --upgrade pip wheel
# Web + GPIO deps. lgpio is the modern pin factory for gpiozero on bookworm.
pip install "Flask>=3.0,<4.0" "gpiozero>=2.0" "lgpio>=0.2"

# --------------------------------------------------------------------------- #
# Build pjproject + PJSUA2 python bindings into the venv
# --------------------------------------------------------------------------- #
if python -c "import pjsua2" 2>/dev/null; then
  echo "==> pjsua2 already available in venv, skipping build"
else
  echo "==> Building pjproject $PJ_VERSION with PJSUA2 python bindings"
  cd "$BUILD_DIR"
  if [[ ! -d "pjproject-$PJ_VERSION" ]]; then
    curl -fsSL -o "pjproject-$PJ_VERSION.tar.gz" \
      "https://github.com/pjsip/pjproject/archive/refs/tags/$PJ_VERSION.tar.gz"
    tar xzf "pjproject-$PJ_VERSION.tar.gz"
  fi
  cd "pjproject-$PJ_VERSION"

  # Position-independent code is required for the python extension module.
  export CFLAGS="-fPIC -O2"
  ./configure --enable-shared --disable-video --disable-libyuv
  make dep
  make
  make install
  ldconfig

  # Build + install the SWIG python bindings into the active venv.
  cd pjsip-apps/src/swig/python
  make
  # setup.py places pjsua2 into the venv's site-packages (python is the venv).
  python setup.py install

  cd "$SRC_DIR"
  python -c "import pjsua2; print('pjsua2 built OK')"
fi

deactivate

# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
if [[ ! -f "$CFG_DIR/config.json" ]]; then
  echo "==> Creating default config (admin/admin)"
  cp "$SRC_DIR/config/config.example.json" "$CFG_DIR/config.json"
  chmod 600 "$CFG_DIR/config.json"
else
  echo "==> Keeping existing $CFG_DIR/config.json"
fi

# --------------------------------------------------------------------------- #
# systemd service
# --------------------------------------------------------------------------- #
echo "==> Installing systemd service"
cp "$SRC_DIR/setup/sip-intercom.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable sip-intercom.service
systemctl restart sip-intercom.service

echo
echo "==> Done."
echo "    Web UI:   http://<pi-ip>:8080   (login admin / admin — change it!)"
echo "    Audio:    set up your USB sound card / I2S HAT per setup/README-audio.md"
echo "    Logs:     journalctl -u sip-intercom -f"
