#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# DEMO-M1: one-click demo of the full Asterinas RISC-V system.
#
# Assembles the complete rootfs (systemd PID 1 + graphical desktop + a nix
# profile spliced into the systemd environment), re-packs the U-Boot boot disk
# with the current kernel + initramfs + DTB, boots it in QEMU (bochs framebuffer
# chain), and collects the demo artifacts:
#
#   target/demo/systemd-boot.log      systemd startup transcript (ANSI-stripped)
#   target/demo/asterinas-desktop.ppm raw QEMU screendump
#   target/demo/asterinas-desktop.png desktop render (converted from the PPM)
#
# The kernel is NOT rebuilt (see the "kernel unchanged" boundary in the DEMO-M1
# report); it must already exist as an Sv39 Image at
#   target/osdk/aster-kernel-osdk-bin.Image
# (override with ASTERINAS_RISCV_BOOTI=).
#
# Usage:
#   tools/riscv/demo-all.sh                        # build + boot + screenshot
#   tools/riscv/demo-all.sh --skip-build           # reuse the existing initramfs
#   tools/riscv/demo-all.sh --settle-seconds 60    # extra render time before the shot
#   tools/riscv/demo-all.sh --collect-timeout 300  # boot collection timeout

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/systemd"
BUILD_SCRIPT="${SRC_DIR}/build_systemd_desktop_nix.sh"
BOOT_DRIVER="${SRC_DIR}/boot_systemd_nixos.py"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${REPO_ROOT}/target/qemu-uboot/systemd-desktop-nix-initramfs.cpio"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
UBOOT="${REPO_ROOT}/target/qemu-uboot/cache/u-boot-build/u-boot"

DEMO_DIR="${REPO_ROOT}/target/demo"
PPM="${DEMO_DIR}/asterinas-desktop.ppm"
PNG="${DEMO_DIR}/asterinas-desktop.png"
BOOT_LOG="${DEMO_DIR}/systemd-boot.log"

SKIP_BUILD=0
COLLECT_TIMEOUT=300
# The boot driver finishes collecting once Xorg adds its keyboard (~39 s), but
# the session clients connect and render well after that. Settle ~60 s so the
# screendump captures the fully-rendered desktop, not a black framebuffer.
SETTLE_SECONDS=60

while [[ $# -gt 0 ]]; do
    case "$1" in
        --skip-build) SKIP_BUILD=1; shift ;;
        --collect-timeout) COLLECT_TIMEOUT="$2"; shift 2 ;;
        --settle-seconds) SETTLE_SECONDS="$2"; shift 2 ;;
        -h|--help) sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

die() { echo "demo-all: $*" >&2; exit 2; }

# --- prerequisites ---------------------------------------------------------
command -v qemu-system-riscv64 >/dev/null 2>&1 || die "qemu-system-riscv64 not found"
[[ -s "${KERNEL_IMAGE}" ]] || die "missing kernel Image (build with: make kernel TARGET_ARCH=riscv64 FEATURES=riscv_sv39_mode; or set ASTERINAS_RISCV_BOOTI=)"
[[ -s "${UBOOT}" ]] || die "missing U-Boot (run: tools/riscv/prepare_qemu_uboot_booti.sh prepare)"
[[ -s "${DTB}" ]] || die "missing DTB: ${DTB}"

# --- 1. build the full rootfs (systemd + desktop + nix) --------------------
if [[ "${SKIP_BUILD}" -eq 1 ]]; then
    [[ -s "${INITRAMFS}" ]] || die "no initramfs to reuse: ${INITRAMFS}"
    echo "=== skipping build; reusing ${INITRAMFS} ==="
else
    echo "=== 1/4 building rootfs (systemd + desktop + nix) ==="
    bash "${BUILD_SCRIPT}"
fi
[[ -s "${INITRAMFS}" ]] || die "initramfs not produced: ${INITRAMFS}"

# --- 2. re-pack the boot disk (kernel + initramfs + DTB) -------------------
echo "=== 2/4 re-packing ${BOOT_DISK} ==="
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

# --- 3. boot in QEMU and capture the serial log + screendump ---------------
echo "=== 3/4 booting QEMU (systemd + desktop + nix) ==="
mkdir -p "${DEMO_DIR}"
python3 "${BOOT_DRIVER}" \
    --collect-timeout "${COLLECT_TIMEOUT}" \
    --settle-seconds "${SETTLE_SECONDS}" \
    --screenshot "${PPM}" \
    --serial-log "${BOOT_LOG}" \
    || die "boot driver reported failure (see ${BOOT_LOG})"

# --- 4. post-process: strip ANSI + convert PPM -> PNG ----------------------
echo "=== 4/4 post-processing demo artifacts ==="
# Strip ANSI/VT control sequences so the log is plain text:
#   - CSI color/attr codes, classic `[0;1;32m` and colon-style `[38:5:185m`,
#     plus private-mode `[?25h` / `[?7h`;
#   - OSC sequences `]104...\a` (reset background);
#   - DECSTR `[!p` (soft reset).
sed \
    -e $'s/\x1b\\[[0-9;:?]*[A-Za-z]//g' \
    -e $'s/\x1b\\][^\x07]*\x07//g' \
    -e $'s/\x1b\\[!p//g' \
    "${BOOT_LOG}" > "${BOOT_LOG}.clean"
mv "${BOOT_LOG}.clean" "${BOOT_LOG}"

ppm_to_png() {
    local ppm="$1" png="$2"
    if command -v magick >/dev/null 2>&1; then
        magick "${ppm}" "${png}"
    elif command -v convert >/dev/null 2>&1; then
        convert "${ppm}" "${png}"
    else
        echo "WARNING: ImageMagick not found; keeping ${ppm} (no PNG)" >&2
    fi
}
if [[ -s "${PPM}" ]]; then
    ppm_to_png "${PPM}" "${PNG}"
fi

echo ""
echo "===== DEMO-M1 artifacts ====="
echo "  boot log:        ${BOOT_LOG}"
echo "  screenshot PPM:  ${PPM}"
echo "  screenshot PNG:  ${PNG}"
echo ""
echo "To view the desktop:  xdg-open ${PNG}   (or any image viewer)"
echo "To re-read the log:   less ${BOOT_LOG}"
echo ""
echo "Tip: for an interactive window, boot with"
echo "  python3 tools/riscv/systemd/boot_systemd_desktop.py  # see README-DEMO.md"
