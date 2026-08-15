#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Cross-compile the pure-X11 desktop panel (xpanel.c) into a static riscv64
# binary and install it into the cross prefix where build_systemd_desktop.sh
# picks it up. The panel needs only libX11 (static), so it links against the
# x11.pc static closure (-lX11 -lpthread -lxcb -lXau).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
CROSS_USR="${REPO_ROOT}/target/riscv-cross/usr"
CC="riscv64-linux-gnu-gcc"
STRIP="riscv64-linux-gnu-strip"

cd "${REPO_ROOT}"

"${CC}" -O2 -static -no-pie \
    -I "${CROSS_USR}/include" \
    tools/riscv/xorg/xpanel.c \
    -o "${CROSS_USR}/bin/xpanel" \
    -L "${CROSS_USR}/lib" \
    -lX11 -lpthread -lxcb -lXau

"${STRIP}" --strip-unneeded "${CROSS_USR}/bin/xpanel"

echo "built ${CROSS_USR}/bin/xpanel ($(stat -c%s "${CROSS_USR}/bin/xpanel") bytes)"
