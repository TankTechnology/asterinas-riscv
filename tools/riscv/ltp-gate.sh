#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# One-command LTP syscall gate for Asterinas on RISC-V (issue #14).
#
# Cross-compiles the LTP syscall subset (tools/riscv/nixos/ltp/build_ltp.sh),
# re-packs the U-Boot boot disk with the freshly built initramfs + kernel Image,
# then boots it in QEMU at SMP=1 and SMP=4 and reports pass/fail per tier.
#
# Usage:
#   tools/riscv/ltp-gate.sh              # build LTP + run SMP=1 and SMP=4
#   tools/riscv/ltp-gate.sh --smp 1      # only one tier
#   tools/riscv/ltp-gate.sh --skip-build # reuse the existing initramfs

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

LTP_DIR="${REPO_ROOT}/tools/riscv/nixos/ltp"
BOOT_DRIVER="${LTP_DIR}/boot_ltp_gate.py"
BUILD_SCRIPT="${LTP_DIR}/build_ltp.sh"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${REPO_ROOT}/target/ltp/ltp-initramfs.cpio.gz"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${REPO_ROOT}/target/ltp/ltp-gate-serial.log"

SKIP_BUILD=0
SMP_TIERS="1 4"
REBUILD_KERNEL=0

usage() {
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smp) SMP_TIERS="$2"; shift 2 ;;
        --skip-build) SKIP_BUILD=1; shift ;;
        --rebuild-kernel) REBUILD_KERNEL=1; shift ;;
        --kernel) KERNEL_IMAGE="$2"; shift 2 ;;
        -h|--help) usage ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

# 0. Optionally rebuild the kernel (for kernel-fix iterations).
if [[ "${REBUILD_KERNEL}" -eq 1 ]]; then
    echo "=== rebuilding kernel (cargo osdk build) ==="
    (cd kernel && cargo osdk build --scheme riscv --release)
    KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
fi

# 1. Build the LTP initramfs (cross-compile + pack).
if [[ "${SKIP_BUILD}" -eq 0 ]]; then
    bash "${BUILD_SCRIPT}"
fi

[[ -s "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -s "${INITRAMFS}" ]] || { echo "missing initramfs: ${INITRAMFS}" >&2; exit 2; }
[[ -s "${DTB}" ]] || { echo "missing DTB: ${DTB}" >&2; exit 2; }

# 2. Re-pack boot.ext4 with the current kernel + initramfs + DTB.
echo "=== re-packing ${BOOT_DISK} ==="
STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${INITRAMFS}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
INITRD_BYTES=$(wc -c < "${INITRAMFS}")
KERNEL_BYTES=$(wc -c < "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 64*1024*1024) / 1024 / 1024 + 1 ))
FLOOR_MB=128
if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"

# 3. Run the gate at each SMP tier.
OVERALL=0
for smp in ${SMP_TIERS}; do
    echo ""
    echo "===== LTP gate: SMP=${smp} ====="
    if python3 "${BOOT_DRIVER}" --smp "${smp}" --serial-log "${SERIAL_LOG}.smp${smp}"; then
        echo "===== SMP=${smp}: PASS ====="
    else
        echo "===== SMP=${smp}: FAIL ====="
        OVERALL=1
    fi
done

echo ""
if [[ "${OVERALL}" -eq 0 ]]; then
    echo "LTP gate: ALL PASS"
else
    echo "LTP gate: FAILURES PRESENT"
fi
exit "${OVERALL}"
