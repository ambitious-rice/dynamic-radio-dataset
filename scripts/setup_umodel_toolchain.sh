#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="${1:-/share1/fzj/tools/umodel_toolchain}"
DEB_DIR="$INSTALL_ROOT/debs"
SYSROOT_DIR="$INSTALL_ROOT/sysroot"
SRC_DIR="$INSTALL_ROOT/src"
UEVIEWER_DIR="$SRC_DIR/UEViewer"

mkdir -p "$DEB_DIR" "$SYSROOT_DIR" "$SRC_DIR" "$INSTALL_ROOT/dynlib"

if [ ! -f "$DEB_DIR/libsdl2-dev_2.0.10+dfsg1-3_amd64.deb" ]; then
  (
    cd "$DEB_DIR"
    apt download libsdl2-dev
  )
fi

dpkg-deb -x "$DEB_DIR"/libsdl2-dev_*.deb "$SYSROOT_DIR"

if [ ! -d "$UEVIEWER_DIR/.git" ]; then
  git clone --depth 1 https://github.com/gildor2/UEViewer.git "$UEVIEWER_DIR"
fi

ln -sf /usr/lib/x86_64-linux-gnu/libSDL2-2.0.so.0 "$INSTALL_ROOT/dynlib/libSDL2.so"

(
  cd "$UEVIEWER_DIR"
  env \
    C_INCLUDE_PATH="$SYSROOT_DIR/usr/include:$SYSROOT_DIR/usr/include/x86_64-linux-gnu" \
    CPLUS_INCLUDE_PATH="$SYSROOT_DIR/usr/include:$SYSROOT_DIR/usr/include/x86_64-linux-gnu" \
    LIBRARY_PATH="$INSTALL_ROOT/dynlib" \
    PKG_CONFIG_PATH="$SYSROOT_DIR/usr/lib/x86_64-linux-gnu/pkgconfig" \
    ./build.sh
)

echo "[OK] Built umodel: $UEVIEWER_DIR/umodel"
