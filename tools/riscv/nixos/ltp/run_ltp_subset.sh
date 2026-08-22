#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Run a hand-picked subset of the LTP syscall tests (fast re-baseline for one or
# a few tests without the ~1 h full gate). Reuses the already-cross-compiled
# binaries in target/ltp/rootfs, filters /opt/ltp/runtest/syscalls down to the
# given tags, re-packs a small initramfs, re-packs boot.ext4 with the current
# kernel Image, and boots it via boot_ltp_gate.py.
#
# Usage:
#   tools/riscv/nixos/ltp/run_ltp_subset.sh readlink03 readlinkat02 timerfd01
#   tools/riscv/nixos/ltp/run_ltp_subset.sh --smp 4 --fork09 ...

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "${REPO_ROOT}"

ROOTFS="${REPO_ROOT}/target/ltp/rootfs"
SUBSET_ROOTFS="${REPO_ROOT}/target/ltp/subset-rootfs"
OUTPUT="${REPO_ROOT}/target/ltp/ltp-subset-initramfs.cpio.gz"
BOOT_DRIVER="${REPO_ROOT}/tools/riscv/nixos/ltp/boot_ltp_gate.py"
KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${REPO_ROOT}/target/ltp/ltp-subset-serial.log"

SMP=1
COMMAND_TIMEOUT=300
TAGS=()

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smp) SMP="$2"; shift 2 ;;
        --command-timeout) COMMAND_TIMEOUT="$2"; shift 2 ;;
        *) TAGS+=("$1"); shift ;;
    esac
done

[[ ${#TAGS[@]} -gt 0 ]] || { echo "usage: $0 [--smp N] <tag> [tag...]" >&2; exit 2; }
[[ -d "${ROOTFS}" ]] || { echo "missing LTP rootfs ${ROOTFS} — run build_ltp.sh first" >&2; exit 2; }

echo "=== subset: ${TAGS[*]} ==="
rm -rf "${SUBSET_ROOTFS}"
cp -a "${ROOTFS}" "${SUBSET_ROOTFS}"

MANIFEST="${SUBSET_ROOTFS}/opt/ltp/runtest/syscalls"
FILTERED="$(mktemp)"
: > "${FILTERED}"
for t in "${TAGS[@]}"; do
    if grep -qE "^${t}[[:space:]]" "${MANIFEST}"; then
        grep -E "^${t}[[:space:]]" "${MANIFEST}" >> "${FILTERED}"
    else
        echo "WARN: no manifest entry for '${t}'" >&2
    fi
done
mv "${FILTERED}" "${MANIFEST}"
N_TESTS=$(grep -cE '\S' "${MANIFEST}")
echo "manifest: ${N_TESTS} tests"

( cd "${SUBSET_ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT}"

[[ -s "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -s "${DTB}" ]] || { echo "missing DTB: ${DTB}" >&2; exit 2; }

echo "=== re-packing ${BOOT_DISK} ==="
STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
truncate -s 128M "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "re-packed ${BOOT_DISK}"

echo ""
echo "===== LTP subset (${TAGS[*]}): SMP=${SMP} ====="
python3 "${BOOT_DRIVER}" --smp "${SMP}" --serial-log "${SERIAL_LOG}.smp${SMP}" \
    --command-timeout "${COMMAND_TIMEOUT}"
