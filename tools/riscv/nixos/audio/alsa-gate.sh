#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# One-command ALSA gate: build the aplay initramfs (Alpine prebuilt ALSA
# userspace + 440 Hz test tone), re-pack the U-Boot boot disk with the current
# kernel + initramfs + DTB, then boot it in QEMU with a virtio-sound device and
# verify the host WAV backend received an audible 440 Hz tone.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
cd "${REPO_ROOT}"

AUDIO_DIR="${REPO_ROOT}/tools/riscv/nixos/audio"
BOOT_DRIVER="${AUDIO_DIR}/boot_alsa.py"
BUILD_SCRIPT="${AUDIO_DIR}/build_alsa.sh"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${REPO_ROOT}/target/nixos/audio/alsa-initramfs.cpio.gz"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${REPO_ROOT}/target/nixos/audio/alsa-serial.log"
WAV_OUT="${REPO_ROOT}/target/nixos/audio/alsa-out.wav"

SMP=1
REBUILD_KERNEL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --smp) SMP="$2"; shift 2 ;;
        --rebuild-kernel) REBUILD_KERNEL=1; shift ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ "${REBUILD_KERNEL}" -eq 1 ]]; then
    echo "=== rebuilding kernel (cargo osdk build) ==="
    RUSTOBJCOPY_DIR="$(dirname "$(find "${HOME}/.rustup/toolchains" -name rust-objcopy -type f 2>/dev/null | head -1)")"
    export PATH="${RUSTOBJCOPY_DIR}:${PATH}"
    export VDSO_LIBRARY_DIR="${VDSO_LIBRARY_DIR:-${HOME}/.local/share/linux_vdso}"
    (cd kernel && OSDK_TARGET_ARCH=riscv64 cargo osdk build --scheme riscv --features riscv_sv39_mode)
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
echo "===== ALSA: SMP=${SMP} ====="
if python3 "${BOOT_DRIVER}" --smp "${SMP}" --serial-log "${SERIAL_LOG}.smp${SMP}" --wav "${WAV_OUT}.smp${SMP}"; then
    echo "===== ALSA: PASS ====="
else
    echo "===== ALSA: FAIL ====="
    exit 1
fi
