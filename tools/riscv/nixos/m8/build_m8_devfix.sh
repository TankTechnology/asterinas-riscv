#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Assemble the devtmpfs auto-create regression boot disk. The initramfs
# deliberately contains **no `/dev`** directory —
# the kernel must create it itself (device::init_in_first_process) instead of
# panicking ("path resolution did not reach the final target").
#
#   boot disk: target/nixos/m8/devtmpfs-nodev/boot.ext4
#
# Usage: bash build_m8_devfix.sh

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
OUT="${REPO_ROOT}/target/nixos/m8/devtmpfs-nodev"
CC="${RISC_V_CC:-riscv64-linux-gnu-gcc}"
QEMU="${QEMU_SYSTEM_RISCV64:-qemu-system-riscv64}"

KERNEL_IMAGE="${ASTERINAS_KERNEL_IMAGE:-}"
DTB="${OUT}/qemu-virt.dtb"

mkdir -p "${OUT}"

# Build the current checkout by default. An explicit image is accepted only
# with a matching SHA-256, which keeps deliberate RED runs reproducible without
# silently packaging an unrelated cached kernel.
if [[ -n "${KERNEL_IMAGE}" ]]; then
    : "${ASTERINAS_KERNEL_IMAGE_SHA256:?set ASTERINAS_KERNEL_IMAGE_SHA256 for an explicit image}"
    printf '%s  %s\n' "${ASTERINAS_KERNEL_IMAGE_SHA256}" "${KERNEL_IMAGE}" | sha256sum --check --status
else
    RUSTOBJCOPY_DIR="$(dirname "$(find "${HOME}/.rustup/toolchains" -name rust-objcopy -type f 2>/dev/null | head -1)")"
    export PATH="${RUSTOBJCOPY_DIR}:${PATH}"
    export VDSO_LIBRARY_DIR="${VDSO_LIBRARY_DIR:-${HOME}/.local/share/linux_vdso}"
    ( cd "${REPO_ROOT}/kernel" && \
        OSDK_TARGET_ARCH=riscv64 cargo osdk build --scheme riscv --features riscv_sv39_mode )
    KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
fi

sha256sum "${KERNEL_IMAGE}" > "${OUT}/kernel-image.sha256"
SOURCE_REVISION="${ASTERINAS_SOURCE_REVISION:-}"
if [[ -z "${SOURCE_REVISION}" ]] && git -C "${REPO_ROOT}" rev-parse --git-dir >/dev/null 2>&1; then
    SOURCE_REVISION="$(git -C "${REPO_ROOT}" rev-parse HEAD)"
    if ! git -C "${REPO_ROOT}" diff --quiet --ignore-submodules --; then
        DIFF_SHA256="$(git -C "${REPO_ROOT}" diff --binary | sha256sum | cut -d' ' -f1)"
        SOURCE_REVISION="${SOURCE_REVISION}+dirty-${DIFF_SHA256}"
    fi
fi
: "${SOURCE_REVISION:?set ASTERINAS_SOURCE_REVISION when Git metadata is unavailable}"
printf '%s\n' "${SOURCE_REVISION}" > "${OUT}/source-revision.txt"

# 1. cross-compile the /init (static glibc)
"${CC}" -O2 -Wall -Wextra -Werror -ffreestanding -fno-builtin \
    -fno-stack-protector -nostdlib -static -no-pie -Wl,-e,_start \
    -o "${OUT}/nodev-init" "${SRC_DIR}/nodev_init.c"

# 2. Minimal initramfs with **no /dev** (only /proc /sys /tmp so the init can
#    breathe). This is the exact condition that used to panic the kernel.
BUILD_TMP="$(mktemp -d)"
trap 'rm -rf -- "${BUILD_TMP}"' EXIT
INITROOT="${BUILD_TMP}/initroot"
mkdir -p "${INITROOT}/proc" "${INITROOT}/sys" "${INITROOT}/tmp"
cp "${OUT}/nodev-init" "${INITROOT}/init"
( cd "${INITROOT}" && find . | cpio -o -H newc 2>/dev/null > "${OUT}/initramfs.cpio" )

# 3. Generate the DTB from the exact machine configuration used by the boot
#    harness. Reusing a stale single-HART DTB with `-smp 4` makes the OpenSBI
#    bootstrap HART nondeterministically disagree with `/cpus`.
"${QEMU}" \
    -machine "virt,dumpdtb=${DTB}" \
    -cpu rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true \
    -m 2G -smp 4 -display none -nodefaults

# 4. Boot disk: current kernel + initramfs + matching QEMU virt DTB.
[[ -f "${KERNEL_IMAGE}" ]] || { echo "missing kernel image: ${KERNEL_IMAGE}" >&2; exit 2; }
[[ -f "${DTB}" ]] || { echo "missing DTB: ${DTB}" >&2; exit 2; }

BOOT_STAGE="${BUILD_TMP}/boot"
mkdir -p "${BOOT_STAGE}"
cp "${KERNEL_IMAGE}" "${BOOT_STAGE}/asterinas.booti"
cp "${OUT}/initramfs.cpio" "${BOOT_STAGE}/initramfs.cpio"
cp "${DTB}" "${BOOT_STAGE}/qemu-virt.dtb"
rm -f "${OUT}/boot.ext4"
truncate -s 128M "${OUT}/boot.ext4"
mkfs.ext4 -q -F -d "${BOOT_STAGE}" "${OUT}/boot.ext4"

echo "built devtmpfs auto-create regression boot disk under ${OUT}"
