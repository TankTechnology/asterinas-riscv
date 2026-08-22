#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M7 daemon: assemble an initramfs that starts nix-daemon and runs a multi-user
# nix build through it, then re-pack the boot disk.
#
# Reuses the M6 rootfs (prebuilt riscv64 musl Nix 2.31.5 + its 45-package
# closure + /m6 payload) and layers the multi-user pieces on top:
#   - build users (nixbld1/nixbld2) + a client user (alice) in /etc/passwd
#   - a multi-user nix.conf (`build-users-group = nixbld`)
#   - three derivations (trivial / whoami / hello) under /m7
#   - a new /init that forks the daemon and drives the client build
#
# The raw newc cpio is written uncompressed (the kernel's zune-inflate decoder
# hangs on >16 MB gzip inputs — M3-report.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
M6_ROOTFS="${NIXOS_ROOT}/m6/rootfs"
M7_ROOTFS="${NIXOS_ROOT}/m7/daemon-rootfs"
OUTPUT="${NIXOS_ROOT}/m7/m7-daemon-initramfs.cpio"

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

# 0. Ensure the M6 rootfs exists (runs build_m6.sh if nix is absent).
if [[ ! -x "${M6_ROOTFS}/usr/bin/nix" ]]; then
    echo "M6 rootfs missing; running build_m6.sh"
    bash "${SRC_DIR}/../m6/build_m6.sh"
fi

# 1. Start from a copy of the M6 rootfs.
rm -rf "${M7_ROOTFS}"
cp -a "${M6_ROOTFS}" "${M7_ROOTFS}"

# 2. Multi-user identity: build users + a client user. musl reads /etc/passwd
#    and /etc/group directly (no NSS modules), so plain entries suffice.
cat > "${M7_ROOTFS}/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
nobody:x:65534:65534:nobody:/:/sbin/nologin
nixbld1:x:30001:30000:Nix build user 1:/var/empty:/sbin/nologin
nixbld2:x:30002:30000:Nix build user 2:/var/empty:/sbin/nologin
alice:x:1000:1000:alice:/tmp:/bin/sh
EOF
cat > "${M7_ROOTFS}/etc/group" <<'EOF'
root:x:0:
nixbld:x:30000:nixbld1,nixbld2
alice:x:1000:
EOF
mkdir -p "${M7_ROOTFS}/var/empty"

# 3. busybox applets the build needs (id for whoami.nix; su for manual checks).
for applet in id groups su; do
    [[ -e "${M7_ROOTFS}/bin/${applet}" ]] || ln -sf busybox "${M7_ROOTFS}/bin/${applet}"
done

# 4. Multi-user nix.conf. `build-users-group = nixbld` is what switches nix
#    into multi-user mode (the daemon drops builder privileges to a nixbld
#    member). seccomp filter stays off (kernel gap, M6-report.md).
mkdir -p "${M7_ROOTFS}/etc/nix"
cat > "${M7_ROOTFS}/etc/nix/nix.conf" <<'EOF'
sandbox = false
build-users-group = nixbld
trusted-users = root
experimental-features = nix-command flakes
filter-syscalls = false
EOF

# 5. M7 payload: derivations + cross-compiled hello (path B).
mkdir -p "${M7_ROOTFS}/m7"
cp "${SRC_DIR}/trivial.nix" "${SRC_DIR}/whoami.nix" "${SRC_DIR}/hello.nix" \
    "${M7_ROOTFS}/m7/"
cp "${SRC_DIR}/../m6/hello.c" "${M7_ROOTFS}/m7/hello.c"
"${CC_MUSL}" -O2 "${SRC_DIR}/../m6/hello.c" -o "${M7_ROOTFS}/m7/hello-prebuilt"
echo "hello-prebuilt: $(file -b "${M7_ROOTFS}/m7/hello-prebuilt" | cut -c1-60)"

# 6. /init launcher (static glibc).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${M7_ROOTFS}/init" "${SRC_DIR}/init_m7_daemon.c"

# 7. Pack as newc cpio (uncompressed).
( cd "${M7_ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT}"
du -sh "${M7_ROOTFS}"

# 8. Re-pack the boot disk.
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
