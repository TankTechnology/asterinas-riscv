#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the AUDIO-M1 initramfs: a single static /init that opens the
# virtio-sound playback node and writes a sine-wave PCM burst.

set -euo pipefail

# shellcheck source=../../../lib/shell.sh
_shell_self_dir=${BASH_SOURCE[0]%/*}
if [[ ${_shell_self_dir} == "${BASH_SOURCE[0]}" ]]; then _shell_self_dir=.; fi
source "${_shell_self_dir}/../../../lib/shell.sh"
unset _shell_self_dir
REPO_ROOT="$(repo_root)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/audio"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/audio-initramfs.cpio.gz}"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init.c" -lm

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
