#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M6: assemble an initramfs that runs `nix build` of a real derivation, then
# re-pack the boot disk.
#
# Reuses the M3 rootfs (prebuilt riscv64 musl Nix 2.31.5 + its 45-package
# closure) and adds the M6 payload: a trivial derivation, a hello derivation
# whose output is a runnable binary, and a new /init.
#
# hello (path B) is cross-compiled on the host as a PIE riscv64 musl binary
# (`hello-prebuilt`) and installed into /nix/store by the derivation's builder.
# Compiling hello in the guest with the Alpine gcc (path A, `--with-gcc`) is
# blocked by a kernel gap: gcc/cc1 are non-PIE (ET_EXEC) and Asterinas does not
# yet execute dynamically-linked ET_EXEC binaries.
#
# The raw newc cpio is written uncompressed: the kernel's zune-inflate decoder
# hangs non-deterministically on >16 MB gzip inputs (M3-report.md).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
M3_ROOTFS="${NIXOS_ROOT}/m3/rootfs"
M6_BUILD_ROOT="${NIXOS_ROOT}/m6"
M6_ROOTFS="${M6_BUILD_ROOT}/rootfs"
APK_CACHE="${NIXOS_ROOT}/m3/apks"
INDEX_CACHE="${NIXOS_ROOT}/m3/apkindex"
OUTPUT="${M6_BUILD_ROOT}/m6-initramfs.cpio.gz"

BOOT_DISK="${REPO_ROOT}/target/qemu-uboot/current/boot.ext4"
KERNEL_IMAGE="${REPO_ROOT}/target/osdk/aster-kernel-osdk-bin.Image"
DTB="${REPO_ROOT}/target/qemu-uboot/current/qemu-virt.dtb"
# 96M covers the toolchain-less initramfs; the repack step auto-sizes up from
# the initramfs + kernel sizes, so this is only a floor.
BOOT_SIZE=96M

CC_STATIC="riscv64-linux-gnu-gcc"   # host cross toolchain for the /init (static)
CC_MUSL="riscv64-linux-musl-gcc"    # host cross toolchain for hello-prebuilt (PIE)

WITH_GCC=0
NO_REPACK=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --with-gcc) WITH_GCC=1 ;;
        --no-repack) NO_REPACK=1 ;;
        --boot-size) BOOT_SIZE="$2"; shift ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
    shift
done

mkdir -p "${M6_BUILD_ROOT}"

# 0. Ensure the M3 rootfs exists (runs build_m3.sh if the nix binary is absent;
#    the 45 .apks are already cached, so this is cheap on a warm tree).
if [[ ! -x "${M3_ROOTFS}/usr/bin/nix" ]]; then
    echo "M3 rootfs missing; running build_m3.sh"
    bash "${SRC_DIR}/../m3/build_m3.sh"
fi

# 1. Start from a copy of the M3 rootfs.
rm -rf "${M6_ROOTFS}"
cp -a "${M3_ROOTFS}" "${M6_ROOTFS}"

# 2. M6 payload.
mkdir -p "${M6_ROOTFS}/m6"
cp "${SRC_DIR}/trivial.nix" "${SRC_DIR}/hello.nix" "${SRC_DIR}/hello-gcc.nix" \
    "${SRC_DIR}/hello.c" "${M6_ROOTFS}/m6/"

# 2a. Cross-compile hello on the host (path B) as a PIE musl binary.
"${CC_MUSL}" -O2 "${SRC_DIR}/hello.c" -o "${M6_ROOTFS}/m6/hello-prebuilt"
echo "hello-prebuilt: $(file -b "${M6_ROOTFS}/m6/hello-prebuilt" | cut -c1-60)"

# 2b. A few extra busybox applets the hello builder needs (cp/chmod). M3 only
# symlinked sh/ls/cat/echo/mkdir/mount/... — cp and chmod are added here.
for applet in cp chmod mv rm; do
    [[ -e "${M6_ROOTFS}/bin/${applet}" ]] || ln -sf busybox "${M6_ROOTFS}/bin/${applet}"
done

# 2c. Nix build config. `filter-syscalls = false` is required: nix installs a
# seccomp filter on the builder by default, and Asterinas does not implement
# seccomp BPF (SECCOMP_SET_MODE_FILTER -> EINVAL). This is the seccomp bypass
# anticipated in the M-plan ("沙箱 (bubblewrap/seccomp) 评估 — 可先绕过").
mkdir -p "${M6_ROOTFS}/etc/nix"
cat > "${M6_ROOTFS}/etc/nix/nix.conf" <<'EOF'
sandbox = false
build-users-group =
trusted-users = root
experimental-features = nix-command flakes
filter-syscalls = false
EOF

# 3. (path A, optional) toolchain for building hello from source in the guest.
if [[ "${WITH_GCC}" -eq 1 ]]; then
    MIRROR="https://mirrors.tuna.tsinghua.edu.cn/alpine/edge"
    declare -A INDEX_FILES
    for repo in main community testing; do
        idx="${INDEX_CACHE}/${repo}-APKINDEX"
        INDEX_FILES[${repo}]="${idx}"
    done
    RESOLVE_ARGS=()
    for repo in main community testing; do
        RESOLVE_ARGS+=(--index "${repo}=${INDEX_FILES[${repo}]}")
    done
    TC_CLOSURE="${M6_BUILD_ROOT}/toolchain-closure.json"
    python3 "${SRC_DIR}/../m3/resolve_deps.py" \
        --root gcc --root binutils --root make --root musl-dev \
        "${RESOLVE_ARGS[@]}" --json > "${TC_CLOSURE}"

    python3 - "${TC_CLOSURE}" "${APK_CACHE}" "${MIRROR}" <<'PY'
import json, os, subprocess, sys
closure_path, cache, mirror = sys.argv[1], sys.argv[2], sys.argv[3]
closure = json.load(open(closure_path))
for pkg in closure:
    dest = os.path.join(cache, pkg["filename"])
    if os.path.exists(dest) and os.path.getsize(dest) > 0:
        continue
    url = f"{mirror}/{pkg['repo']}/riscv64/{pkg['filename']}"
    print(f"fetching {pkg['filename']}")
    subprocess.run(["curl", "-fsSL", "--retry", "3", "-o", dest, url], check=True)
print(f"{len(closure)} toolchain packages cached")
PY

    python3 - "${TC_CLOSURE}" "${APK_CACHE}" "${M6_ROOTFS}" <<'PY'
import json, os, subprocess, sys
closure_path, cache, rootfs = sys.argv[1], sys.argv[2], sys.argv[3]
closure = json.load(open(closure_path))
EXCLUDES = (
    "--exclude=.PKGINFO", "--exclude=.SIGN.*", "--exclude=.trigger",
    "--exclude=.pre-install", "--exclude=.post-install",
    "--exclude=.pre-upgrade", "--exclude=.post-upgrade",
    "--exclude=.pre-deinstall", "--exclude=.post-deinstall",
)
for pkg in closure:
    apk = os.path.join(cache, pkg["filename"])
    subprocess.run(["tar", "-xzf", apk, "-C", rootfs, *EXCLUDES], check=True)
print(f"extracted {len(closure)} toolchain packages into rootfs")
PY
    echo "toolchain installed:"
    file -b "${M6_ROOTFS}/usr/bin/gcc" | cut -c1-60
    file -b "${M6_ROOTFS}/usr/bin/ld" | cut -c1-60

    # Prune the LTO + C++-modules frontends and profiling libs. `gcc hello.c
    # -o hello` only needs cc1 + collect2 + as + ld; lto1/lto-wrapper/
    # liblto_plugin are ~55 MB and are only used under -flto.
    GCC_LIBEXEC="${M6_ROOTFS}/usr/libexec/gcc/riscv64-alpine-linux-musl/15.2.0"
    rm -f "${GCC_LIBEXEC}/lto1" "${GCC_LIBEXEC}/lto-wrapper" \
          "${GCC_LIBEXEC}/liblto_plugin.so" "${GCC_LIBEXEC}/g++-mapper-server"
    rm -rf "${GCC_LIBEXEC}/plugin" "${GCC_LIBEXEC}/install-tools"
    GCC_LIB="${M6_ROOTFS}/usr/lib/gcc/riscv64-alpine-linux-musl/15.2.0"
    rm -rf "${GCC_LIB}/plugin" "${GCC_LIB}/install-tools"
    rm -f "${GCC_LIB}/libgcov.a" "${GCC_LIB}/libcaf_single.a"
fi

# 4. /init launcher (static glibc, same pattern as M1-M5).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${M6_ROOTFS}/init" "${SRC_DIR}/init_m6.c"

# 5. Pack as newc cpio (uncompressed).
( cd "${M6_ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

echo "built ${OUTPUT}"
du -sh "${M6_ROOTFS}"

# 6. Re-pack the boot disk with the new initramfs (kernel Image and DTB are
#    unchanged from M1-M5).
if [[ "${NO_REPACK}" -eq 0 ]]; then
    STAGE="$(mktemp -d)"
    cp "${KERNEL_IMAGE}" "${STAGE}/asterinas.booti"
    cp "${OUTPUT}" "${STAGE}/initramfs.cpio.gz"
    cp "${DTB}" "${STAGE}/qemu-virt.dtb"
    rm -f "${BOOT_DISK}"
    # Auto-size: initramfs + kernel + 32M headroom, at least BOOT_SIZE.
    INITRD_BYTES=$(stat -c%s "${OUTPUT}")
    KERNEL_BYTES=$(stat -c%s "${KERNEL_IMAGE}")
    BOOT_MB=$(( (INITRD_BYTES + KERNEL_BYTES + 32*1024*1024) / 1024 / 1024 + 1 ))
    FLOOR_MB=${BOOT_SIZE%M}
    if (( BOOT_MB < FLOOR_MB )); then BOOT_MB=${FLOOR_MB}; fi
    truncate -s "${BOOT_MB}M" "${BOOT_DISK}"
    mkfs.ext4 -q -F -d "${STAGE}" "${BOOT_DISK}"
    rm -rf "${STAGE}"
    echo "re-packed ${BOOT_DISK} (${BOOT_MB}M)"
fi
