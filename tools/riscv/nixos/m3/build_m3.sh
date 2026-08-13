#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# M3: assemble an initramfs carrying Nix (2.31.5) and its full musl riscv64
# dependency closure, sourced from the TUNA Alpine edge mirror (direct, proxy-free).
#
# Strategy (see M3-report.md): instead of cross-compiling Nix from source, we
# reuse Alpine's prebuilt riscv64 musl `nix` package and every .so it links.
# resolve_deps.py walks the APKINDEX dependency graph to compute the install
# closure; each .apk is a gzip'd tar that extracts straight into the rootfs.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NIXOS_ROOT="${REPO_ROOT}/target/nixos"
BUILD_ROOT="${NIXOS_ROOT}/m3"
ROOTFS="${BUILD_ROOT}/rootfs"
APK_CACHE="${BUILD_ROOT}/apks"
INDEX_CACHE="${BUILD_ROOT}/apkindex"
OUTPUT="${1:-${BUILD_ROOT}/m3-initramfs.cpio.gz}"

MIRROR="https://mirrors.tuna.tsinghua.edu.cn/alpine/edge"
readonly -a REPOS=(main community testing)
CC_STATIC="riscv64-linux-gnu-gcc"

mkdir -p "${BUILD_ROOT}" "${APK_CACHE}" "${INDEX_CACHE}"

# 1. Fetch APKINDEX metadata (cached).
declare -A INDEX_FILES
for repo in "${REPOS[@]}"; do
    idx="${INDEX_CACHE}/${repo}-APKINDEX"
    if [[ ! -s "${idx}" ]]; then
        echo "fetching ${repo} APKINDEX"
        curl -fsSL --retry 3 -o "${idx}.tar.gz" \
            "${MIRROR}/${repo}/riscv64/APKINDEX.tar.gz"
        tar -xzf "${idx}.tar.gz" -C "${INDEX_CACHE}"
        mv "${INDEX_CACHE}/APKINDEX" "${idx}"
        rm -f "${idx}.tar.gz"
    fi
    INDEX_FILES[${repo}]="${idx}"
done

# 2. Resolve the dependency closure.
RESOLVE_ARGS=()
for repo in "${REPOS[@]}"; do
    RESOLVE_ARGS+=(--index "${repo}=${INDEX_FILES[${repo}]}")
done
CLOSURE_JSON="${BUILD_ROOT}/closure.json"
python3 "${SRC_DIR}/resolve_deps.py" --root nix "${RESOLVE_ARGS[@]}" --json \
    > "${CLOSURE_JSON}"

# 3. Download every .apk (cached).
python3 - "${CLOSURE_JSON}" "${APK_CACHE}" "${MIRROR}" <<'PY'
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
print(f"{len(closure)} packages cached")
PY

# 4. Assemble the rootfs.
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}"
python3 - "${CLOSURE_JSON}" "${APK_CACHE}" "${ROOTFS}" <<'PY'
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
print(f"extracted {len(closure)} packages into rootfs")
PY

# 4b. Prune test-only binaries and libraries that the `nix` CLI never links
# (nix-*-tests, gtest/gmock/rapidcheck test-support). apk pulls them in at the
# package level, but they add ~9 MB and no value for the smoke test.
rm -f "${ROOTFS}"/usr/bin/nix-*-tests
rm -f "${ROOTFS}"/usr/lib/libnix-*-test-support.so*
rm -f "${ROOTFS}"/usr/lib/libgtest* "${ROOTFS}"/usr/lib/libgmock* \
      "${ROOTFS}"/usr/lib/librapidcheck*

# 5. Base filesystem plumbing that Alpine's triggers/scripts would normally do.
#    busybox applet symlinks (the package only ships bin/busybox).
ln -sf busybox "${ROOTFS}/bin/sh"
for applet in ls cat echo mkdir mount umount ps sleep uname env stat head; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done

# Minimal identity files (musl reads /etc/passwd directly for getpwuid).
cat > "${ROOTFS}/etc/passwd" <<'EOF'
root:x:0:0:root:/root:/bin/sh
nobody:x:65534:65534:nobody:/:/sbin/nologin
EOF
cat > "${ROOTFS}/etc/group" <<'EOF'
root:x:0:
nixbld:x:30000:
EOF
cat > "${ROOTFS}/etc/hosts" <<'EOF'
127.0.0.1 localhost localhost.localdomain
::1       localhost localhost.localdomain
EOF

# Nix store + state (ramfs is writable; nix opens/creates its SQLite state here).
mkdir -p "${ROOTFS}/nix/store" "${ROOTFS}/nix/var/nix" "${ROOTFS}/root"

# Mount points for the pseudo-filesystems (M1/M2 created these explicitly; the
# kernel's first-process stdio setup opens /dev/console, so /dev must exist).
mkdir -p "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

# Nix config for an unprivileged, sandbox-less smoke test. `nix eval` requires
# the `nix-command` experimental feature (the `nix` CLI is gated behind it).
cat > "${ROOTFS}/etc/nix/nix.conf" <<'EOF'
sandbox = false
build-users-group =
trusted-users = root
experimental-features = nix-command flakes
EOF

# 6. /init launcher (static glibc, same pattern as M1/M2).
"${CC_STATIC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_m3.c"

# 7. Pack as newc cpio (uncompressed).
#
# gzip (-9) would shrink the ~36 MB rootfs to ~16 MB, but the kernel's
# zune-inflate decoder hangs non-deterministically on >16 MB inputs on this
# build (see M3-report.md); the raw cpio decodes directly and boots reliably.
# The `.cpio.gz` suffix is retained only to match the boot pipeline's payload
# name convention (initramfs.cpio.gz in the ext4 boot disk).
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null > "${OUTPUT}" )

echo "built ${OUTPUT}"
echo "  nix: $(file -b "${ROOTFS}/usr/bin/nix" | cut -c1-60)"
du -sh "${ROOTFS}"
