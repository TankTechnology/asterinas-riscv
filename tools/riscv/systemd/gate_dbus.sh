#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# XFCE-M1 D-Bus gate: assemble the systemd+desktop initramfs, re-pack the
# U-Boot boot disk, boot it in QEMU, and verify that the D-Bus system bus
# starts and answers ListNames calls.
#
# Usage:
#   tools/riscv/systemd/gate_dbus.sh                  # build initramfs + boot
#   tools/riscv/systemd/gate_dbus.sh --collect-timeout 300

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

SRC_DIR="${REPO_ROOT}/tools/riscv/systemd"
BOOT_DRIVER="${SRC_DIR}/boot_systemd_desktop.py"
BUILD_SCRIPT="${SRC_DIR}/build_systemd_desktop.sh"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${REPO_ROOT}/target/qemu-uboot/systemd-desktop-initramfs.cpio"
BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
SERIAL_LOG="${REPO_ROOT}/target/systemd-desktop/dbus-serial.log"

COLLECT_TIMEOUT=120
while [[ $# -gt 0 ]]; do
    case "$1" in
        --collect-timeout) COLLECT_TIMEOUT="$2"; shift 2 ;;
        --no-build) shift ;;
        -h|--help) sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if ! ls "${INITRAMFS}" >/dev/null 2>&1; then
    echo "=== building initramfs ==="
    bash "${BUILD_SCRIPT}" "${INITRAMFS}"
fi

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
echo "===== XFCE-M1 D-BUS GATE ====="
python3 "${BOOT_DRIVER}" \
    --collect-timeout "${COLLECT_TIMEOUT}" \
    --serial-log "${SERIAL_LOG}" \
    --smp 1 \
    --loglevel warn
BOOT_RC=$?

echo ""
echo "===== D-Bus verification ====="

# D-Bus system bus markers (ANSI-stripped for systemd colorized output)
DBUS_STARTED=0
DBUS_SMOKE=0
DBUS_FAILED=0

if grep -a "Started.*D-Bus System Message Bus\|D-Bus System Message Bus" "${SERIAL_LOG}" >/dev/null 2>&1; then
    DBUS_STARTED=1
    echo "  dbus.service: STARTED"
else
    echo "  dbus.service: MISSING"
fi

# The smoke test output: method return from org.freedesktop.DBus
if grep -a "method return.*sender=org\.freedesktop\.DBus\|string.*org\.freedesktop\.DBus\|Finished.*D-Bus system bus smoke test" "${SERIAL_LOG}" >/dev/null 2>&1; then
    DBUS_SMOKE=1
    echo "  dbus-smoke.service: ListNames REPLY received (PASS)"
else
    echo "  dbus-smoke.service: no ListNames reply"
fi

if grep -a "Failed to start D-Bus\|dbus.service.*failed\|dbus.*exit-code\|dbus.*status=1" "${SERIAL_LOG}" >/dev/null 2>&1; then
    DBUS_FAILED=1
    echo "  dbus.service: FAILED"
fi

# Print the relevant serial log lines
echo ""
echo "=== D-Bus serial log excerpt ==="
grep -a -i -E "dbus|D-Bus|ListNames|org\.freedesktop" "${SERIAL_LOG}" 2>/dev/null | head -20 || echo "(no matches)"

echo ""
echo "=== tail of serial log ==="
tail -40 "${SERIAL_LOG}" 2>/dev/null || echo "(empty)"

echo ""
echo "=== Gate result ==="
if [[ $DBUS_STARTED -eq 1 && $DBUS_SMOKE -eq 1 ]]; then
    echo "[XFCE-M1] D-Bus system bus: PASS"
    exit 0
elif [[ $DBUS_STARTED -eq 1 ]]; then
    echo "[XFCE-M1] D-Bus system bus started but smoke test failed: PARTIAL"
    exit 1
else
    echo "[XFCE-M1] D-Bus system bus: FAIL"
    exit 1
fi