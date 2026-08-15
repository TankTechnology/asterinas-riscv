#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DRM-M10: assemble the systemd-desktop initramfs + xbench render benchmark, and
# re-pack an independent /tmp/m10-desktop boot disk. Unlike M8, this regenerates
# the qemu-virt DTB with BOTH `-smp N` AND `-m 2G` so the guest's memory map
# matches what QEMU actually provides (the M8/M9 DTB shipped QEMU's default
# 128 MB memory node, which is what broke the smp=4 boot).
#
#   /tmp/m10-desktop/boot.ext4  (kernel + initramfs + dtb, ext4)
#
# Usage: bash build_m10_desktop.sh [--smp N] [--no-repack]

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DESKTOP_TREE="${DESKTOP_TREE:-$HOME/Program/asterinas-riscv}"
DESKTOP_ROOTFS="${DESKTOP_TREE}/target/systemd-desktop/rootfs"
CROSS_USR="${DESKTOP_TREE}/target/riscv-cross/usr"
MODESETTING_SO="${DESKTOP_TREE}/target/riscv-cross/src/xserver/build/hw/xfree86/drivers/modesetting/modesetting_drv.so"

BUILD_ROOT="${REPO_ROOT}/target/nixos/m10"
ROOTFS="${BUILD_ROOT}/rootfs"
OUTPUT="${BUILD_ROOT}/initramfs.cpio"

DISK_DIR="/tmp/m10-desktop"
BOOT_DISK="${DISK_DIR}/boot.ext4"
U_BOOT="${DISK_DIR}/u-boot"
DTB="${DISK_DIR}/qemu-virt.dtb"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
XBENCH="${SRC_DIR}/../m9/xbench"

CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
SMP=4
NO_REPACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --smp) SMP="$2"; shift 2 ;;
        --no-repack) NO_REPACK=1; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

[[ -d "${DESKTOP_ROOTFS}" ]] || { echo "missing desktop rootfs: ${DESKTOP_ROOTFS}" >&2; exit 2; }
[[ -f "${MODESETTING_SO}" ]] || { echo "missing modesetting driver: ${MODESETTING_SO}" >&2; exit 2; }
[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -f "${XBENCH}" ]] || { echo "missing xbench: ${XBENCH} — run tools/riscv/nixos/m9/build_xbench.sh first" >&2; exit 2; }

mkdir -p "${DISK_DIR}"

# --- 0. u-boot + a CORRECT 4-CPU / 2G DTB -----------------------------------
if [[ ! -f "${U_BOOT}" ]]; then
    cp "${DESKTOP_TREE}/target/qemu-uboot/cache/u-boot-build/u-boot" "${U_BOOT}"
fi
echo "regenerating qemu-virt.dtb (smp=${SMP}, m=2G)"
qemu-system-riscv64 -machine virt -m 2G -smp "${SMP}" \
    -cpu 'rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true' \
    -machine "dumpdtb=${DTB}" -nographic >/dev/null 2>&1 || true
echo "  cpus: $(dtc -I dtb -O dts "${DTB}" 2>/dev/null | grep -c 'cpu@')"

echo "=== assembling DRM-M10 desktop rootfs ==="
mkdir -p "${BUILD_ROOT}"
rm -rf "${ROOTFS}"
cp -a "${DESKTOP_ROOTFS}" "${ROOTFS}"

# --- 1. DRM modesetting driver + libdrm (alongside the bundled fbdev) --------
mkdir -p "${ROOTFS}/usr/lib/xorg/modules/drivers"
cp "${MODESETTING_SO}" "${ROOTFS}/usr/lib/xorg/modules/drivers/modesetting_drv.so"
cp -a "${CROSS_USR}/lib/libdrm.so.2.4.0" "${ROOTFS}/usr/lib/libdrm.so.2.4.0"
ln -sf libdrm.so.2.4.0 "${ROOTFS}/usr/lib/libdrm.so.2"
ln -sf libdrm.so.2     "${ROOTFS}/usr/lib/libdrm.so"

# --- 2. Xorg config pair (runtime fallback via /init) ------------------------
cp "${SRC_DIR}/../drm/xorg-modesetting.conf" "${ROOTFS}/etc/xorg-modesetting.conf"
cp "${SRC_DIR}/../m8/xorg-fbdev.conf" "${ROOTFS}/etc/xorg-fbdev.conf"

# --- 3. /init launcher with GPU fallback -------------------------------------
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/../m8/init_drm.c"

# --- 4. xbench + oneshot service --------------------------------------------
cp "${XBENCH}" "${ROOTFS}/usr/bin/xbench"
cat > "${ROOTFS}/etc/systemd/system/xbench.service" <<'UNIT'
[Unit]
Description=X11 render benchmark (fbdev vs modesetting)
After=xorg.service
Requires=xorg.service
PartOf=graphical.target

[Service]
Type=oneshot
Environment=DISPLAY=:0
Environment=HOME=/root
ExecStart=/usr/bin/xbench
StandardOutput=tty
StandardError=tty
RemainAfterExit=yes
UNIT
# Wire xbench into graphical.target's Wants= list.
sed -i 's/\(Wants=.*netsurf.service\)/\1 xbench.service/' \
    "${ROOTFS}/etc/systemd/system/graphical.target"

# --- 5. pack raw newc cpio (no gzip) -----------------------------------------
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT} ($(stat -c%s "${OUTPUT}") bytes)"

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
FLOOR_MB=256
if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
