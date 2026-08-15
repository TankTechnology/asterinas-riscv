#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M5: assemble the full-system integration initramfs and re-pack the
# independent /tmp/drm-m5 boot disk with the merged kernel.
#
# The guest boots the DRM-tree kernel (DRM + ALSA + clock_getres) into the
# sibling asterinas-riscv tree's systemd desktop (Xorg + matchbox-wm + xpanel +
# pcmanfm + xterm + NetSurf), then layers on top of it:
#
#   1. the DRM modesetting Xorg driver + libdrm (so Xorg drives /dev/dri/card0
#      instead of the bochs fbdev framebuffer),
#   2. the Alpine musl aplay + alsa-lib userspace + a oneshot `alsa.service`
#      that plays a 440 Hz / 48 kHz / S16LE / stereo WAV through virtio-sound.
#
# The initramfs is packed as *raw* newc cpio (no gzip): the kernel's
# zune-inflate decoder hangs non-deterministically on >16 MB gzip inputs and
# this rootfs is ~95 MB.
#
# Usage:
#     bash build_m5.sh [--no-repack]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The systemd desktop userspace lives in the sibling asterinas-riscv tree (built
# by its build_systemd_desktop.sh). The DRM modesetting driver + libdrm were
# cross-compiled into that tree's target/riscv-cross prefix during DRM-M3.
DESKTOP_TREE="${DESKTOP_TREE:-$HOME/Program/asterinas-riscv}"
DESKTOP_ROOTFS="${DESKTOP_TREE}/target/systemd-desktop/rootfs"
CROSS_USR="${DESKTOP_TREE}/target/riscv-cross/usr"
MODESETTING_SO="${DESKTOP_TREE}/target/riscv-cross/src/xserver/build/hw/xfree86/drivers/modesetting/modesetting_drv.so"

# The Alpine musl + alsa-lib + alsa-utils APK cache (unpacked .d dirs) was
# populated by the nixos tree's build_alsa.sh.
ALSA_CACHE="${ALSA_CACHE:-$HOME/Program/asterinas-riscv-nixos/target/nixos/audio/alpine}"
MUSL="musl-1.2.5-r12"
ALSA_LIB="alsa-lib-1.2.14-r0"
ALSA_UTILS="alsa-utils-1.2.14-r0"

BUILD_ROOT="${REPO_ROOT}/target/nixos/m5"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${BUILD_ROOT}/initramfs.cpio"

DISK_DIR="/tmp/drm-m5"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"

NO_REPACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-repack) NO_REPACK=1 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

[[ -d "${DESKTOP_ROOTFS}" ]] || { echo "missing desktop rootfs: ${DESKTOP_ROOTFS}" >&2; exit 2; }
[[ -f "${MODESETTING_SO}" ]] || { echo "missing modesetting driver: ${MODESETTING_SO}" >&2; exit 2; }
[[ -d "${ALSA_CACHE}/${MUSL}.d" ]] || { echo "missing ALSA musl cache: ${ALSA_CACHE}/${MUSL}.d" >&2; exit 2; }
[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }

mkdir -p "${DISK_DIR}"

# Seed u-boot + DTB from the proven DRM-M4 disk (byte-identical across the DRM
# milestones; M1 keeps both at the top level).
[[ -f "${U_BOOT}" ]] || cp /tmp/drm-m4/u-boot "${U_BOOT}"
[[ -f "${DTB}" ]] || cp /tmp/drm-m4/qemu-virt.dtb "${DTB}"

echo "=== assembling DRM-M5 integration rootfs ==="
mkdir -p "${BUILD_ROOT}"
rm -rf "${ROOTFS}"
cp -a "${DESKTOP_ROOTFS}" "${ROOTFS}"

# --- 1. DRM modesetting driver + libdrm (replace the fbdev-only desktop) -----
mkdir -p "${ROOTFS}/usr/lib/xorg/modules/drivers"
cp "${MODESETTING_SO}" "${ROOTFS}/usr/lib/xorg/modules/drivers/modesetting_drv.so"
cp -a "${CROSS_USR}/lib/libdrm.so.2.4.0" "${ROOTFS}/usr/lib/libdrm.so.2.4.0"
ln -sf libdrm.so.2.4.0 "${ROOTFS}/usr/lib/libdrm.so.2"
ln -sf libdrm.so.2     "${ROOTFS}/usr/lib/libdrm.so"
# Xorg still loads the evdev input driver and fbdevhw; keep both. The xorg.conf
# swap below is what actually selects modesetting over fbdev.
cp "${SRC_DIR}/../drm/xorg-modesetting.conf" "${ROOTFS}/etc/xorg.conf"

# --- 2. ALSA userspace (musl aplay + alsa-lib) ------------------------------
cp -a "${ALSA_CACHE}/${MUSL}.d/lib/ld-musl-riscv64.so.1" "${ROOTFS}/lib/"
ln -sf ld-musl-riscv64.so.1 "${ROOTFS}/lib/libc.musl-riscv64.so.1"
for lib in libasound.so.2 libasound.so.2.0.0; do
    cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/usr/lib/${lib}" "${ROOTFS}/usr/lib/"
done
cp -a "${ALSA_CACHE}/${ALSA_UTILS}.d/usr/bin/aplay" "${ROOTFS}/usr/bin/aplay"
if [ -d "${ALSA_CACHE}/${ALSA_LIB}.d/usr/share/alsa" ]; then
    mkdir -p "${ROOTFS}/usr/share/alsa"
    cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/usr/share/alsa/." "${ROOTFS}/usr/share/alsa/"
fi
if [ -d "${ALSA_CACHE}/${ALSA_LIB}.d/etc/alsa" ]; then
    mkdir -p "${ROOTFS}/etc/alsa"
    cp -a "${ALSA_CACHE}/${ALSA_LIB}.d/etc/alsa/." "${ROOTFS}/etc/alsa/"
fi

# --- 3. ALSA test launcher (static) + oneshot systemd unit ------------------
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/usr/bin/alsa-test" "${SRC_DIR}/alsa_test.c"
cp "${SRC_DIR}/alsa.service" "${ROOTFS}/etc/systemd/system/alsa.service"
mkdir -p "${ROOTFS}/etc/systemd/system/multi-user.target.wants"
ln -sf ../alsa.service "${ROOTFS}/etc/systemd/system/multi-user.target.wants/alsa.service"

# --- 4. 440 Hz test tone ----------------------------------------------------
python3 - "${ROOTFS}/sine.wav" <<'PY'
import math, struct, sys, wave
rate, ch, sec, freq = 48000, 2, 1, 440.0
frames = rate * sec
data = bytearray()
for i in range(frames):
    s = int(16383.0 * math.sin(2.0 * math.pi * freq * i / rate))
    for _ in range(ch):
        data += struct.pack("<h", s)
with wave.open(sys.argv[1], "wb") as w:
    w.setnchannels(ch); w.setsampwidth(2); w.setframerate(rate)
    w.writeframes(bytes(data))
print(f"generated {sys.argv[1]} ({len(data)} PCM bytes @ {rate} Hz, {ch} ch)")
PY

# --- 5. pack raw newc cpio (no gzip) ----------------------------------------
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT}"

if [[ "${NO_REPACK}" -eq 1 ]]; then
    echo "assembled rootfs (--no-repack): ${ROOTFS}"
    exit 0
fi

STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
rm -f "${BOOT_DISK}"
INITRD_BYTES=$(stat -c%s "${OUTPUT}")
KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
FLOOR_MB=128
if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
