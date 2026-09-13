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
# FOUNDATION-M2-report.md. The musl sysroot lacks the Linux UAPI headers, so the
# GNU cross include directory is searched with `-idirafter`: musl must provide
# libc headers such as stdio.h and the GNU tree is only a fallback for Linux
# UAPI namespaces absent from musl. Only the tests enabled by the selected
# closed suite are packed, crossed against LTP's runtest/syscalls manifest.
#
# Options:
#   --skip-compile   reuse already-built LTP binaries (fast re-pack)
#   --suite NAME     package syscalls (default) or arch-riscv64

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../../../.." && pwd)"
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LTP_SRC="${REPO_ROOT}/target/ltp/src"
ROOTFS="${REPO_ROOT}/target/ltp/rootfs"
OUTPUT="${REPO_ROOT}/target/ltp/ltp-initramfs.cpio.gz"
STAGE="${REPO_ROOT}/target/ltp/stage"
BUSYBOX="${REPO_ROOT}/target/nixos/busybox"
PACKAGE_IDENTITY="${REPO_ROOT}/target/ltp/package.json"
SCHED_VARIANT_PATCH="${SRC_DIR}/sched_setscheduler04-variant-getters.patch"
CLONE_RAW_PATCH="${SRC_DIR}/cloner-riscv-raw-clone.patch"

CC="riscv64-linux-musl-gcc"
STRIP="riscv64-linux-gnu-strip"
MUSL_ROOT="/usr/riscv64-linux-musl"
MUSL_LIBC="${MUSL_ROOT}/lib/musl/lib/libc.so"
GNU_UAPI_ROOT="/usr/riscv64-linux-gnu/include"
JOBS="${JOBS:-16}"
SUITE="syscalls"
SKIP_COMPILE=0

while [[ "$#" -gt 0 ]]; do
    case "$1" in
        --skip-compile) SKIP_COMPILE=1; shift ;;
        --suite)
            if [[ "$#" -lt 2 ]]; then
                echo "--suite requires a value" >&2
                exit 2
            fi
            SUITE="$2"; shift 2
            ;;
        *) echo "unknown arg: $1" >&2; exit 2 ;;
    esac
done

if [[ "${ASTERINAS_LTP_PACKAGE_LOCK_HELD:-0}" != 1 ]]; then
    gate=(
        python3 "${REPO_ROOT}/tools/riscv/ltp_gate.py" build
        --suite "${SUITE}"
    )
    if [[ "${SKIP_COMPILE}" -eq 1 ]]; then
        gate+=(--skip-compile)
    fi
    exec "${gate[@]}"
fi

mapfile -t SUITE_FIELDS < <(
    python3 "${REPO_ROOT}/tools/riscv/ltp_suite.py" describe \
        --repo "${REPO_ROOT}" --suite "${SUITE}"
)
if [[ "${#SUITE_FIELDS[@]}" -ne 3 ]]; then
    echo "failed to resolve LTP suite: ${SUITE}" >&2
    exit 2
fi
ENABLED_TESTS="${SUITE_FIELDS[0]}"
EXPECTED_SELECTED="${SUITE_FIELDS[1]}"
EXPECTED_UNAVAILABLE="${SUITE_FIELDS[2]}"

rm -f "${PACKAGE_IDENTITY}"

required_tools=("${CC}" "${STRIP}")
if [[ "${SKIP_COMPILE}" -eq 0 ]]; then
    required_tools+=(aclocal autoconf automake git)
fi
for tool in "${required_tools[@]}"; do
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
if [[ ! -x "${BUSYBOX}" ]]; then
    echo "missing required BusyBox at ${BUSYBOX}" >&2
    echo "build it with: tools/riscv/nixos/build_busybox.sh" >&2
    exit 2
fi

if [[ "${SKIP_COMPILE}" -eq 0 ]]; then
    # Musl's legacy scheduler wrappers are ENOSYS stubs, and its public
    # clone() rejects thread-management flags before entering the kernel.
    # Keep these LTP cases on raw syscalls so they test Asterinas itself.
    for patch in "${SCHED_VARIANT_PATCH}" "${CLONE_RAW_PATCH}"; do
        patch_name="$(basename "${patch}")"
        if git -C "${LTP_SRC}" apply --unidiff-zero --check "${patch}"; then
            git -C "${LTP_SRC}" apply --unidiff-zero "${patch}"
        elif git -C "${LTP_SRC}" apply --unidiff-zero --reverse --check "${patch}"; then
            echo "LTP patch already applied: ${patch_name}"
        else
            echo "LTP patch does not apply cleanly: ${patch_name}" >&2
            exit 2
        fi
    done

    echo "=== configure LTP for riscv64 (musl, dynamic, cross) ==="
    cd "${LTP_SRC}"
    make autotools >/dev/null 2>&1 || { echo "make autotools failed" >&2; exit 2; }
    # Drop stale objects from any prior (e.g. static glibc/musl) configure so a
    # CC/libc/flag switch does not leave a mixed build.
    [ -f Makefile ] && make clean >/dev/null 2>&1 || true
    CC="${CC}" DEBUG_CFLAGS="" \
        CFLAGS="-O2 -fno-stack-protector -fPIC -idirafter ${GNU_UAPI_ROOT}" \
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
    --enabled "${ENABLED_TESTS}" \
    --runtest "${LTP_SRC}/runtest/syscalls" \
    --bin-dir "${STAGE}/opt/ltp/testcases/bin" \
    --output "${FILTERED}" \
    --unavailable-output "${UNAVAILABLE}" \
    --expected-count "${EXPECTED_SELECTED}"

ACTUAL_UNAVAILABLE="$(grep -c '"name"' "${UNAVAILABLE}" || true)"
if [[ "${ACTUAL_UNAVAILABLE}" -ne "${EXPECTED_UNAVAILABLE}" ]]; then
    echo "expected ${EXPECTED_UNAVAILABLE} unavailable tests, got ${ACTUAL_UNAVAILABLE}" >&2
    exit 2
fi

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
mkdir -p "${ROOTFS}/bin"
cp -f "${BUSYBOX}" "${ROOTFS}/bin/busybox"
for applet in sh cat true echo test cp; do
    ln -sf busybox "${ROOTFS}/bin/${applet}"
done
echo "busybox applets: $(ls "${ROOTFS}/bin")"

# Copy LTP resource files (helper binaries) that the Makefile install
# target doesn't include but tests need at runtime.
for pair in \
    "execve/execve_child" \
    "execveat/execveat_child" \
    "execveat/execveat_errno" \
    "openat/openat02_child"; do
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
echo "suite: ${SUITE}"

echo "=== build /init and /ltp_runner (static musl) ==="
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/init" "${SRC_DIR}/init_ltp.c"
"${CC}" -O2 -static -no-pie -fno-stack-protector \
    -o "${ROOTFS}/ltp_runner" "${SRC_DIR}/ltp_runner.c"

echo "=== pack initramfs ==="
( cd "${ROOTFS}" && find . | cpio -o -H newc 2>/dev/null | gzip -9 > "${OUTPUT}" )
python3 "${REPO_ROOT}/tools/riscv/ltp_package.py" publish \
    --suite "${SUITE}" \
    --initramfs "${OUTPUT}" \
    --manifest "${FILTERED}" \
    --unavailable "${UNAVAILABLE}" \
    --output "${PACKAGE_IDENTITY}"
echo "built ${OUTPUT} ($(wc -c < "${OUTPUT}") bytes, ${N_TESTS} tests)"
