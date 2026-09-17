#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M9: cross-compile the X11 render micro-benchmark (xbench.c) into a static
# riscv64 binary, so the fbdev (bochs) and modesetting (virtio-gpu) drivers can
# be compared on the same guest. Links against the sibling tree's pre-built
# libX11 static closure (-lX11 -lpthread -lxcb -lXau), mirroring
# tools/riscv/xorg/build_xpanel.sh.

set -euo pipefail

DESKTOP_TREE="${DESKTOP_TREE:-$HOME/Program/asterinas-riscv}"
CROSS_USR="${DESKTOP_TREE}/target/riscv-cross/usr"

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${SRC_DIR}/xbench"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
STRIP="riscv64-linux-gnu-strip"

[[ -f "${CROSS_USR}/lib/libX11.a" ]] || { echo "missing libX11.a in ${CROSS_USR}/lib" >&2; exit 2; }

"${CC}" -O2 -static -no-pie \
    -I "${CROSS_USR}/include" \
    "${SRC_DIR}/xbench.c" \
    -o "${OUT}" \
    -L "${CROSS_USR}/lib" \
    -lX11 -lpthread -lxcb -lXau

"${STRIP}" --strip-unneeded "${OUT}"

echo "built ${OUT} ($(stat -c%s "${OUT}") bytes)"
