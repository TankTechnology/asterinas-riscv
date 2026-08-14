#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# One-command FOUNDATION-M3 gate: build the fm3 smoke initramfs, re-pack the
# U-Boot boot disk with the current kernel + initramfs + DTB, then boot it in
# QEMU and report per-test pass/fail.
#
# Usage:
#   tools/riscv/fm3-gate.sh                     # build initramfs + run
#   tools/riscv/fm3-gate.sh --rebuild-kernel    # rebuild kernel first
#   tools/riscv/fm3-gate.sh --smp 4             # SMP=4

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

FM3_DIR="${REPO_ROOT}/tools/riscv/nixos/fm3"
BOOT_DRIVER="${FM3_DIR}/boot_fm3.py"
BUILD_SCRIPT="${FM3_DIR}/build_fm3.sh"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${REPO_ROOT}/target/nixos/fm3/fm3-initramfs.cpio.gz"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${REPO_ROOT}/target/nixos/fm3/fm3-serial.log"

SMP=1
REBUILD_KERNEL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smp) SMP="$2"; shift 2 ;;
        --rebuild-kernel) REBUILD_KERNEL=1; shift ;;
        --kernel) KERNEL_IMAGE="$2"; shift 2 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ "${REBUILD_KERNEL}" -eq 1 ]]; then
    echo "=== rebuilding kernel (cargo osdk build) ==="
    # The RISC-V Image step shells out to `rust-objcopy`, which is only
    # installed inside the rustup toolchain sysroot (not on PATH). The target
    # arch must be forced to riscv64 (else OSDK builds the host x86_64 arch),
    # and the sv39 feature must match the boot driver's `sv48=false` CPU.
    RUSTOBJCOPY_DIR="$(dirname "$(find "${HOME}/.rustup/toolchains" -name rust-objcopy -type f 2>/dev/null | head -1)")"
    export PATH="${RUSTOBJCOPY_DIR}:${PATH}"
    export VDSO_LIBRARY_DIR="${VDSO_LIBRARY_DIR:-${HOME}/.local/share/linux_vdso}"
    (cd kernel && OSDK_TARGET_ARCH=riscv64 cargo osdk build --scheme riscv --features riscv_sv39_mode --release)
    KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
fi

bash "${BUILD_SCRIPT}"

[[ -s "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -s "${INITRAMFS}" ]] || { echo "missing initramfs: ${INITRAMFS}" >&2; exit 2; }
[[ -s "${DTB}" ]] || { echo "missing DTB: ${DTB}" >&2; exit 2; }

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

echo ""
echo "===== FOUNDATION-M3: SMP=${SMP} ====="
if python3 "${BOOT_DRIVER}" --smp "${SMP}" --serial-log "${SERIAL_LOG}.smp${SMP}"; then
    echo "===== FOUNDATION-M3: PASS ====="
else
    echo "===== FOUNDATION-M3: FAIL ====="
    exit 1
fi
