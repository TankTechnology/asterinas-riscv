#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M9: assemble the lightweight-NixOS demo rootfs and re-pack the boot disk.
#
# Reuses the M8 light-rootfs (prebuilt riscv64 musl Nix 2.31.5 + closure +
# busybox, single-user nix.conf) and layers on a real PID-1 system:
#   - a new /init (mounts, hostname, exec busybox init)
#   - /etc/inittab (sysinit rc + getty/login respawn loop on ttyS0)
#   - /etc/rc + /etc/init.d/S* service management (syslogd, crond, and a
#     nix-derivation-driven heartbeat daemon)
#   - a two-generation nix profile: core tools (cross-compiled on the host)
#     then real curl + jq (prebuilt closure fetched from the Alpine mirror)
#
# All in-guest software is *prebuilt*: the four core tools are cross-compiled
# here with riscv64-linux-musl-gcc, and curl/jq are extracted from Alpine
# riscv64 APKs. Nothing is compiled in the guest (gcc is still blocked by the
# ET_EXEC + PT_INTERP loader gap — M6/M8 reports).
#
# The raw newc cpio is written uncompressed (the kernel's zune-inflate decoder
# hangs on >16 MB gzip inputs — M3-report.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
M8_ROOTFS="${NIXOS_ROOT}/m8/light-rootfs"
M9_ROOTFS="${NIXOS_ROOT}/m9/rootfs"
M9_APKS="${NIXOS_ROOT}/m9/apks"
OUTPUT="${NIXOS_ROOT}/m9/m9-initramfs.cpio"

BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

CC_STATIC="riscv64-linux-gnu-gcc"   # host cross toolchain for the /init (static)
CC_MUSL="riscv64-linux-musl-gcc"    # host cross toolchain for the core tools (PIE)

NO_REPACK=0
while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-repack) NO_REPACK=1 ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

if ! command -v "${CC_STATIC}" >/dev/null 2>&1; then
    echo "missing ${CC_STATIC}; install riscv64-linux-gnu-gcc" >&2; exit 2
fi
if ! command -v "${CC_MUSL}" >/dev/null 2>&1; then
    echo "missing ${CC_MUSL}; install riscv64-linux-musl-gcc" >&2; exit 2
fi

# 0. Ensure the M8 light-rootfs exists (runs build_m8_light.sh if nix is absent).
if [[ ! -x "${M8_ROOTFS}/usr/bin/nix" ]]; then
    echo "M8 light-rootfs missing; running build_m8_light.sh"
    bash "${SRC_DIR}/../m8/build_m8_light.sh"
fi

# 1. Start from a copy of the M8 light-rootfs.
rm -rf "${M9_ROOTFS}"
cp -a "${M8_ROOTFS}" "${M9_ROOTFS}"
mkdir -p "${M9_ROOTFS}/m9" "${M9_ROOTFS}/m9/prebuilt" "${M9_ROOTFS}/m9/pkg"

# 2. Cross-compile the four core tools (PIE musl; these run in the guest).
#    <binary name>:<source file basename>
for pair in "hello:hello" "nixos-info:nixos_info" "fortune:fortune" "heartbeat:heartbeat"; do
    bin="${pair%%:*}"; src="${pair##*:}"
    "${CC_MUSL}" -O2 "${SRC_DIR}/tools/${src}.c" -o "${M9_ROOTFS}/m9/prebuilt/${bin}"
    echo "${bin}: $(file -b "${M9_ROOTFS}/m9/prebuilt/${bin}" | cut -c1-60)"
done

# 3. Prebuilt closure: curl + jq from Alpine riscv64 APKs (already fetched to
#    target/nixos/m9/apks by this repo's README). Extract the binaries and the
#    shared libs that curl/jq need but the base rootfs lacks (libjq, libonig).
MIRROR="https://mirrors.tuna.tsinghua.edu.cn/alpine/edge/main/riscv64"
fetch_apk() { # $1 = filename
    local f="$1"
    if [[ ! -s "${M9_APKS}/${f}" ]]; then
        echo "fetching ${f}"
        curl -fsSL --retry 3 -o "${M9_APKS}/${f}" "${MIRROR}/${f}"
    fi
}
fetch_apk curl-8.21.0-r0.apk
fetch_apk jq-1.8.2-r0.apk
fetch_apk oniguruma-6.9.10-r0.apk

T="$(mktemp -d)"
tar -xzf "${M9_APKS}/curl-8.21.0-r0.apk" -C "${T}" usr/bin/curl
tar -xzf "${M9_APKS}/jq-1.8.2-r0.apk" -C "${T}" usr/bin/jq usr/lib/libjq.so.1 usr/lib/libjq.so.1.0.4
tar -xzf "${M9_APKS}/oniguruma-6.9.10-r0.apk" -C "${T}" usr/lib/libonig.so.5 usr/lib/libonig.so.5.5.0
cp "${T}/usr/bin/curl" "${M9_ROOTFS}/m9/pkg/curl"
cp "${T}/usr/bin/jq"   "${M9_ROOTFS}/m9/pkg/jq"
# The musl shared-library closure for jq goes into the base image /usr/lib
# (curl's libs — libcurl/libssl/libz/… — are already there from nix).
cp -a "${T}/usr/lib/libjq.so.1" "${T}/usr/lib/libjq.so.1.0.4" \
      "${T}/usr/lib/libonig.so.5" "${T}/usr/lib/libonig.so.5.5.0" \
      "${M9_ROOTFS}/usr/lib/"
rm -rf "${T}"
echo "curl: $(file -b "${M9_ROOTFS}/m9/pkg/curl" | cut -c1-60)"
echo "jq:   $(file -b "${M9_ROOTFS}/m9/pkg/jq" | cut -c1-60)"

# 4. Payload: derivations + identity + init system.
cp "${SRC_DIR}/core.nix" "${SRC_DIR}/real.nix" "${M9_ROOTFS}/m9/"
cp "${SRC_DIR}/profile"   "${M9_ROOTFS}/etc/profile"
cp "${SRC_DIR}/passwd"    "${M9_ROOTFS}/etc/passwd"
cp "${SRC_DIR}/group"     "${M9_ROOTFS}/etc/group"
cp "${SRC_DIR}/shadow"    "${M9_ROOTFS}/etc/shadow"
chmod 600 "${M9_ROOTFS}/etc/shadow"
cp "${SRC_DIR}/securetty" "${M9_ROOTFS}/etc/securetty"
cp "${SRC_DIR}/inittab"   "${M9_ROOTFS}/etc/inittab"
cp "${SRC_DIR}/rc"        "${M9_ROOTFS}/etc/rc"
cp "${SRC_DIR}/rc.shutdown" "${M9_ROOTFS}/etc/rc.shutdown"
cp "${SRC_DIR}/motd"      "${M9_ROOTFS}/etc/motd"
chmod +x "${M9_ROOTFS}/etc/rc" "${M9_ROOTFS}/etc/rc.shutdown"
mkdir -p "${M9_ROOTFS}/etc/init.d"
cp "${SRC_DIR}/init.d/S10syslogd" "${SRC_DIR}/init.d/S20crond" \
   "${SRC_DIR}/init.d/S30heartbeat" "${M9_ROOTFS}/etc/init.d/"
chmod +x "${M9_ROOTFS}"/etc/init.d/S*
mkdir -p "${M9_ROOTFS}/var/spool/cron/crontabs"

# 5. Single-user nix.conf (no daemon — M8-report.md). Re-affirm the config.
mkdir -p "${M9_ROOTFS}/etc/nix"
cat > "${M9_ROOTFS}/etc/nix/nix.conf" <<'EOF'
sandbox = false
build-users-group =
trusted-users = root
experimental-features = nix-command flakes
filter-syscalls = false
EOF

# 6. busybox applets the init system needs (getty/login/init/services/…).
for applet in getty login init hostname syslogd crond killall kill reboot halt \
              poweroff tail grep wc head clear passwd adduser addgroup setsid \
              nohup which pidof sync; do
    [[ -e "${M9_ROOTFS}/bin/${applet}" ]] || ln -sf busybox "${M9_ROOTFS}/bin/${applet}"
done
mkdir -p "${M9_ROOTFS}/sbin"
for applet in getty init login reboot halt poweroff; do
    ln -sf ../bin/busybox "${M9_ROOTFS}/sbin/${applet}"
done

# 7. /init launcher (static glibc).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${M9_ROOTFS}/init" "${SRC_DIR}/init_m9.c"

# 8. Pack as newc cpio (uncompressed).
( cd "${M9_ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT}"
du -sh "${M9_ROOTFS}"

# 9. Re-pack the boot disk.
if [[ "${NO_REPACK}" -eq 0 ]]; then
    STAGE="$(mktemp -d)"
    cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
    cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
    cp "${DTB}" "${STAGE}/qemu-virt.dtb"
    rm -f "${BOOT_DISK}"
    INITRD_BYTES=$(stat -c%s "${OUTPUT}")
    KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
    BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
    FLOOR_MB=96
    if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
    truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
    mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
    rm -rf "${STAGE}"
    echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
fi
