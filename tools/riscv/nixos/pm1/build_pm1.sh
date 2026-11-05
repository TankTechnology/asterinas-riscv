#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the POLISH-M1 initramfs: a single static /init smoke binary that
# exercises seccomp SECCOMP_SET_MODE_FILTER (classic BPF).

set -euo pipefail

# shellcheck source=../../../lib/shell.sh
_shell_self_dir=${BASH_SOURCE[0]%/*}
if [[ ${_shell_self_dir} == "${BASH_SOURCE[0]}" ]]; then _shell_self_dir=.; fi
source "${_shell_self_dir}/../../../lib/shell.sh"
unset _shell_self_dir
REPO_ROOT="$(repo_root)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos/pm1"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${1:-${BUILD_ROOT}/pm1-initramfs.cpio.gz}"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init.c"

( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )

echo "built ${OUTPUT}"
