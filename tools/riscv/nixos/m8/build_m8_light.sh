#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M8 lightweight route: assemble an initramfs that boots a busybox-init +
# nix-profile system (no systemd). Reuses the M7 daemon rootfs (prebuilt
# riscv64 musl Nix 2.31.5 + closure + multi-user identities) and layers on:
#   - /etc/profile activation (adds the nix profile bin dir to PATH)
#   - an m8 hello derivation
#   - a new /init that starts nix-daemon, `nix profile install`s hello into
#     /nix/var/nix/profiles/default, then runs a login shell that executes it.
#
# The raw newc cpio is written uncompressed (the kernel's zune-inflate decoder
# hangs on >16 MB gzip inputs — M3-report.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
M7_ROOTFS="${NIXOS_ROOT}/m7/daemon-rootfs"
M8_ROOTFS="${NIXOS_ROOT}/m8/light-rootfs"
OUTPUT="${NIXOS_ROOT}/m8/m8-light-initramfs.cpio"

BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"

CC_STATIC="riscv64-linux-gnu-gcc"
CC_MUSL="riscv64-linux-musl-gcc"

if ! command -v "${CC_STATIC}" >/dev/null 2>&1; then
    echo "missing ${CC_STATIC}; install riscv64-linux-gnu-gcc" >&2; exit 2
fi
if ! command -v "${CC_MUSL}" >/dev/null 2>&1; then
    echo "missing ${CC_MUSL}; install riscv64-linux-musl-gcc" >&2; exit 2
fi

# 0. Ensure the M7 daemon rootfs exists.
if [[ ! -x "${M7_ROOTFS}/usr/bin/nix" ]]; then
    echo "M7 daemon rootfs missing; running build_m7_daemon.sh"
    bash "${SRC_DIR}/../m7/build_m7_daemon.sh"
fi

# 1. Start from a copy of the M7 daemon rootfs.
rm -rf "${M8_ROOTFS}"
cp -a "${M7_ROOTFS}" "${M8_ROOTFS}"

# 2. /etc/profile activation.
cp "${SRC_DIR}/profile" "${M8_ROOTFS}/etc/profile"

# 2b. Single-user nix: drop build-users-group so nix builds locally (no daemon),
#     the genuinely "lightweight" config. Keep the seccomp/sandbox bypasses.
cat > "${M8_ROOTFS}/etc/nix/nix.conf" <<'EOF'
sandbox = false
build-users-group =
trusted-users = root
experimental-features = nix-command flakes
filter-syscalls = false
EOF

# 3. M8 payload: the hello derivation + cross-compiled hello (path B).
mkdir -p "${M8_ROOTFS}/m8"
cp "${SRC_DIR}/hello.nix" "${M8_ROOTFS}/m8/hello.nix"
cp "${SRC_DIR}/../m6/hello.c" "${M8_ROOTFS}/m8/hello.c"
"${CC_MUSL}" -O2 "${SRC_DIR}/../m6/hello.c" -o "${M8_ROOTFS}/m8/hello-prebuilt"
echo "hello-prebuilt: $(file -b "${M8_ROOTFS}/m8/hello-prebuilt" | cut -c1-60)"

# 4. /init launcher (static glibc).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${M8_ROOTFS}/init" "${SRC_DIR}/init_m8_light.c"

# 5. Pack as newc cpio (uncompressed).
( cd "${M8_ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT}"
du -sh "${M8_ROOTFS}"

# 6. Re-pack the boot disk.
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
