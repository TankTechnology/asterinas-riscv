#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# One-command POLISH-M7 ALSA-in-systemd gate: assemble the systemd+ALSA
# initramfs (glibc systemd PID 1 + musl aplay + virtio-sound), re-pack the
# U-Boot boot disk, then boot it in QEMU, log in over getty, and run aplay to
# verify the host WAV backend received an audible 440 Hz tone.
#
# Usage:
#   tools/riscv/systemd/gate_alsa.sh                  # build initramfs + boot
#   tools/riscv/systemd/gate_alsa.sh --smp 4          # SMP=4

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/systemd"
BOOT_DRIVER="${SRC_DIR}/boot_systemd_alsa.py"
BUILD_SCRIPT="${SRC_DIR}/build_systemd_alsa.sh"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${REPO_ROOT}/target/nixos/systemd/systemd-alsa-initramfs.cpio.gz"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${REPO_ROOT}/target/nixos/systemd/systemd-alsa-serial.log"
WAV_OUT="${REPO_ROOT}/target/nixos/systemd/systemd-alsa-out.wav"

SMP=1
while [[ $# -gt 0 ]]; do
    case "$1" in
        --smp) SMP="$2"; shift 2 ;;
        --collect-timeout) COLLECT_TIMEOUT="$2"; shift 2 ;;
        -h|--help) sed -n '2,14p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

bash "${BUILD_SCRIPT}" "${INITRAMFS}"

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
echo "re-packed ${BOOT_DISK} (${BOOT_MB}M, initrd ${INITRD_BYTES} bytes)"

echo ""
echo "===== SYSTEMD-ALSA: SMP=${SMP} ====="
if python3 "${BOOT_DRIVER}" --smp "${SMP}" --serial-log "${SERIAL_LOG}.smp${SMP}" --wav "${WAV_OUT}.smp${SMP}"; then
    echo "===== SYSTEMD-ALSA: PASS ====="
else
    echo "===== SYSTEMD-ALSA: FAIL ====="
    exit 1
fi
