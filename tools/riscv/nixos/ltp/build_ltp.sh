#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Cross-compile the LTP syscall test subset for riscv64 and pack it, together
# with a minimal C runner, into an initramfs for the Asterinas RISC-V gate.
#
# Mirrors upstream's Nix-built LTP gate (test/initramfs/nix/conformance/ltp.nix)
# but without Nix. The test binaries are *dynamically* linked against musl libc
# and a shared libltp.so so the whole suite fits in a ~16 MiB initramfs. Static
# linking (glibc or musl) either blows past the kernel's large-initramfs unpack
# limit or needs a second block device, both blocked kernel bugs — see
# FOUNDATION-M2-report.md. The musl sysroot lacks the Linux UAPI headers, so we
# -isystem the glibc cross sysroot's include dir (musl's own headers win). Only
# the tests enabled in test/initramfs/src/conformance/ltp/testcases/all.txt are
# packed, crossed against LTP's runtest/syscalls manifest.
#
# Options:
#   --skip-compile   reuse already-built LTP binaries (fast re-pack)

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LTP_SRC="${REPO_ROOT}/target/ltp/src"
ROOTFS="${REPO_ROOT}/target/ltp/rootfs"
OUTPUT="${REPO_ROOT}/target/ltp/ltp-initramfs.cpio.gz"
STAGE="${REPO_ROOT}/target/ltp/stage"
ALL_TESTS="${REPO_ROOT}/test/initramfs/src/conformance/ltp/testcases/all.txt"

CC="riscv64-linux-musl-gcc"
STRIP="riscv64-linux-gnu-strip"
MUSL_ROOT="/usr/riscv64-linux-musl"
MUSL_LIBC="${MUSL_ROOT}/lib/musl/lib/libc.so"
GNU_UAPI_ROOT="/usr/riscv64-linux-gnu/include"
JOBS="${JOBS:-16}"
SKIP_COMPILE=0

for arg in "$@"; do
    case "${arg}" in
        --skip-compile) SKIP_COMPILE=1 ;;
        *) echo "unknown arg: ${arg}" >&2; exit 2 ;;
    esac
done

for tool in "${CC}" "${STRIP}" aclocal autoconf automake; do
    if ! command -v "${tool}" >/dev/null 2>&1; then
        echo "missing ${tool}" >&2
        exit 2
    fi
done
for input in "${MUSL_LIBC}" "${GNU_UAPI_ROOT}/linux/limits.h"; do
    if [ ! -f "${input}" ]; then
        echo "missing cross-build input ${input}" >&2
        exit 2
    fi
done
if [ ! -d "${LTP_SRC}" ]; then
    echo "missing LTP source at ${LTP_SRC}" >&2
    echo "clone it with: git clone --depth 1 --branch 20260529 \\" >&2
    echo "    https://github.com/linux-test-project/ltp.git target/ltp/src" >&2
    exit 2
fi

if [[ "${SKIP_COMPILE}" -eq 0 ]]; then
    echo "=== configure LTP for riscv64 (musl, dynamic, cross) ==="
    cd "${LTP_SRC}"
    make autotools >/dev/null 2>&1 || { echo "make autotools failed" >&2; exit 2; }
    # Drop stale objects from any prior (e.g. static glibc/musl) configure so a
    # CC/libc/flag switch does not leave a mixed build.
    [ -f Makefile ] && make clean >/dev/null 2>&1 || true
    CC="${CC}" DEBUG_CFLAGS="" \
        CFLAGS="-O2 -fno-stack-protector -fPIC -isystem ${GNU_UAPI_ROOT}" \
        LDFLAGS="" \
        ./configure --host=riscv64-linux-gnu --prefix=/opt/ltp \
        >/dev/null 2>&1 || { echo "configure failed" >&2; exit 2; }

    echo "=== build libltp (${JOBS} jobs, -fPIC) ==="
    make -C testcases/lib -j"${JOBS}"

    echo "=== link shared libltp.so from PIC objects ==="
    ( cd lib && "${CC}" -shared -o libltp.so ./*.o )

    echo "=== build syscall tests (dynamic, tolerant, ${JOBS} jobs) ==="
    FAIL_LOG="${REPO_ROOT}/target/ltp/build-failures.txt"
    : > "${FAIL_LOG}"
    find testcases/kernel/syscalls -mindepth 1 -maxdepth 1 -type d -print0 | \
        xargs -0 -P "${JOBS}" -I{} sh -c \
        'make -C "$1" all -j1 >/dev/null 2>&1 || echo "$1"' _ {} \
        >> "${FAIL_LOG}"
    echo "$(wc -l < "${FAIL_LOG}") dirs failed to build (see ${FAIL_LOG})"
fi

echo "=== stage install into ${STAGE} ==="
cd "${LTP_SRC}"
rm -rf "${STAGE}"
mkdir -p "${STAGE}/opt/ltp/testcases/bin"
find testcases/kernel/syscalls -mindepth 1 -maxdepth 1 -type d -print0 | \
    xargs -0 -P "${JOBS}" -I{} sh -c \
    'make -C "$1" install DESTDIR="${2}" >/dev/null 2>&1 || true' _ {} "${STAGE}"

echo "=== assemble initramfs rootfs (enabled tests only) ==="
rm -rf "${ROOTFS}"
mkdir -p "${ROOTFS}/opt/ltp/testcases/bin" \
         "${ROOTFS}/opt/ltp/runtest" \
         "${ROOTFS}/opt/ltp/lib" \
         "${ROOTFS}/lib" \
         "${ROOTFS}/etc" \
         "${ROOTFS}/dev" "${ROOTFS}/proc" "${ROOTFS}/sys" "${ROOTFS}/tmp"

# musl getpwnam/getgrnam read these directly; without them LTP's "nobody"
# lookups fail with ENOENT and break most tests.
install -m 0644 "${SRC_DIR}/etc-passwd" "${ROOTFS}/etc/passwd"
install -m 0644 "${SRC_DIR}/etc-group" "${ROOTFS}/etc/group"

# Select the complete validated manifest first. The selector records every
# unavailable test instead of silently dropping it from the runtime evidence.
FILTERED="${ROOTFS}/opt/ltp/runtest/syscalls"
UNAVAILABLE="${REPO_ROOT}/target/ltp/unavailable-tests.json"
rm -f "${FILTERED}" "${UNAVAILABLE}"
python3 "${REPO_ROOT}/tools/riscv/ltp_manifest.py" select \
    --enabled "${ALL_TESTS}" \
    --runtest "${LTP_SRC}/runtest/syscalls" \
    --bin-dir "${STAGE}/opt/ltp/testcases/bin" \
    --output "${FILTERED}" \
    --unavailable-output "${UNAVAILABLE}" \
    --expected-count 767

# Copy only the binaries referenced by the validated manifest.
while read -r tag bin params; do
    cp -f "${STAGE}/opt/ltp/testcases/bin/${bin}" \
        "${ROOTFS}/opt/ltp/testcases/bin/${bin}"
done < "${FILTERED}"
N_TESTS=$(grep -cE '\S' "${FILTERED}")

# Copy the shared libraries the dynamic binaries need: musl libc at both the
# loader path (INTERP) and the library path (NEEDED), plus libltp.so on the
# loader's default search path (/lib) so it resolves without LD_LIBRARY_PATH.
cp -f "${MUSL_LIBC}" "${ROOTFS}/lib/libc.so"
cp -f "${MUSL_LIBC}" "${ROOTFS}/lib/ld-musl-riscv64.so.1"
cp -f "${LTP_SRC}/lib/libltp.so" "${ROOTFS}/lib/libltp.so"
cp -f "${LTP_SRC}/lib/libltp.so" "${ROOTFS}/opt/ltp/lib/libltp.so"

# Several tests shell out to /bin/sh, /bin/cat, /bin/true (access02,
# posix_fadvise03, setrlimit04); without them the tests TBROK on a missing
# helper rather than exercising the kernel. Layer a static busybox (built by
# tools/riscv/nixos/build_busybox.sh) and the applet symlinks those tests use.
BUSYBOX="${REPO_ROOT}/target/nixos/busybox"
if [ -x "${BUSYBOX}" ]; then
    mkdir -p "${ROOTFS}/bin"
    cp -f "${BUSYBOX}" "${ROOTFS}/bin/busybox"
    for a in sh cat true echo test; do
        ln -sf busybox "${ROOTFS}/bin/${a}"
    done
    echo "busybox applets: $(ls "${ROOTFS}/bin")"
else
    echo "WARN: no busybox at ${BUSYBOX} — shell-out tests will TBROK" >&2
fi

# Copy LTP resource files (helper binaries) that the Makefile install
# target doesn't include but tests need at runtime.
for pair in \
    "execve/execve_child" \
    "execveat/execveat_child" \
    "execveat/execveat_errno"; do
    src="${LTP_SRC}/testcases/kernel/syscalls/${pair}"
    if [ -f "${src}" ]; then
        cp -f "${src}" "${ROOTFS}/opt/ltp/testcases/bin/"
    fi
done

# Strip everything.
find "${ROOTFS}/opt/ltp/testcases/bin" -type f -executable \
    -exec "${STRIP}" {} \; 2>/dev/null || true
"${STRIP}" "${ROOTFS}/opt/ltp/lib/libltp.so" 2>/dev/null || true
echo "manifest: ${N_TESTS} enabled tests"

echo "=== build /init and /ltp_runner (static musl) ==="
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_ltp.c"
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/ltp_runner" "${SRC_DIR}/ltp_runner.c"

echo "=== pack initramfs ==="
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
echo "built ${OUTPUT} ($(wc -c < "${OUTPUT}") bytes, ${N_TESTS} tests)"
