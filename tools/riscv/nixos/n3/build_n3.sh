#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# NIXOS-N3: assemble an initramfs with the official glibc Nix 2.30.2 riscv64
# closure (from ~/Program/backups/nix-riscv64/) plus a static busybox shell,
# and pack a private boot disk under /tmp/n3-nix/.
#
# The cpio is written UNCOMPRESSED: the kernel's zune-inflate decoder hangs
# on >16 MB gzip inputs (M3-report.md), and the closure is ~76 MB.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BUILD_ROOT="${REPO_ROOT}/target/nixos"
N3_ROOT="${BUILD_ROOT}/n3"
ROOTFS="${N3_ROOT}/rootfs"
OUTPUT="${N3_ROOT}/n3-initramfs.cpio"

CLOSURE_TARBALL="${N3_CLOSURE_TARBALL:-${HOME}/Program/backups/nix-riscv64/nix-2.30.2-riscv64-linux.tar.xz}"
CLOSURE_STAGE="${N3_ROOT}/closure"

KERNEL_IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image}"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
BUSYBOX="${BUILD_ROOT}/busybox"

DISK_DIR="${N3_DISK_DIR:-/tmp/n3-nix}"
BOOT_DISK="${DISK_DIR}/boot.ext4"

CC="riscv64-linux-gnu-gcc"

[[ -f "${CLOSURE_TARBALL}" ]] || { echo "missing closure tarball: ${CLOSURE_TARBALL}" >&2; exit 2; }
[[ -x "${BUSYBOX}" ]] || { echo "missing ${BUSYBOX}; run build_busybox.sh first" >&2; exit 2; }
[[ -s "${KERNEL_IMAGE}" ]] || { echo "missing kernel Image" >&2; exit 2; }
[[ -s "${DTB}" ]] || { echo "missing DTB" >&2; exit 2; }

# 1. Unpack the closure once (idempotent; the extracted store is ~76 MB).
if [[ ! -d "${CLOSURE_STAGE}/nix-2.30.2-riscv64-linux/store" ]]; then
    rm -rf "${CLOSURE_STAGE}"
    mkdir -p "${CLOSURE_STAGE}"
    tar -xJf "${CLOSURE_TARBALL}" -C "${CLOSURE_STAGE}"
fi

# 2. Assemble the rootfs.
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/bin" "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" \
    "${ROOTFS}/tmp" "${ROOTFS}/root" "${ROOTFS}/etc/nix" \
    "${ROOTFS}/nix/var/nix/db" "${ROOTFS}/nix/var/nix/gcroots" \
    "${ROOTFS}/nix/var/nix/profiles" "${ROOTFS}/nix/var/log/nix"

"${CC}" -O2 -static -no-pie -fno-stack-protector -o "${ROOTFS}/init" "${SRC_DIR}/init_n3.c"
"${CC}" -O2 -static -no-pie -fno-stack-protector -o "${ROOTFS}/bin/netprobe" "${SRC_DIR}/netprobe.c"

cp "${BUSYBOX}" "${ROOTFS}/bin/busybox"
for applet in sh echo cat ls mkdir sleep head mount "["; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done

# The Nix store closure at its canonical /nix/store location.
mkdir -p "${ROOTFS}/nix"
cp -a "${CLOSURE_STAGE}/nix-2.30.2-riscv64-linux/store" "${ROOTFS}/nix/store"
cp "${CLOSURE_STAGE}/nix-2.30.2-riscv64-linux/.reginfo" "${ROOTFS}/nix/.reginfo"

# glibc NSS + identities + resolver (slirp DNS).
cat > "${ROOTFS}/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
nobody:x:65534:65534:nobody:/:/bin/false
EOF
cat > "${ROOTFS}/etc/group" <<'EOF'
root:x:0:
nobody:x:65534:
EOF
cat > "${ROOTFS}/etc/nsswitch.conf" <<'EOF'
passwd: files
group: files
hosts: files dns
EOF
# Workaround for the guest UDP-inbound gap (DNS replies never arrive);
# resolve the substituter statically.
cat > "${ROOTFS}/etc/hosts" <<'EOF'
127.0.0.1 localhost
::1 localhost
199.232.161.91 cache.nixos.org
EOF
cat > "${ROOTFS}/etc/resolv.conf" <<'EOF'
nameserver 10.0.2.3
EOF

# Nix configuration: single-user daemon (empty build-users-group), no sandbox
# (CLONE_NEWNET is a known kernel gap, NIXOS-N3-preflight.md), no seccomp
# filter, no substituters by default (offline first install).
cat > "${ROOTFS}/etc/nix/nix.conf" <<'EOF'
sandbox = false
build-users-group =
experimental-features = nix-command flakes
filter-syscalls = false
substituters =
EOF

# 3. Pack as newc cpio, uncompressed.
mkdir -p "${N3_ROOT}"
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )
echo "built ${OUTPUT} ($(du -h "${OUTPUT}" | cut -f1))"

# 4. Pack the private boot disk.
mkdir -p "${DISK_DIR}"
STAGE="$(mktemp -d)"
cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
cp "${DTB}" "${STAGE}/qemu-virt.dtb"
INITRD_BYTES=$(wc -c < "${OUTPUT}")
KERNEL_BYTES=$(wc -c < "${KERNEL_IMAGE}")
BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 64*1024*1024) / 1024 / 1024 + 1 ))
if (( BOOT_MB < 256 )); then BOOT_MB=256; fi
truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
rm -rf "${STAGE}"
echo "packed ${BOOT_DISK} (${BOOT_MB}M)"
