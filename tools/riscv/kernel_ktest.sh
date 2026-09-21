#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# Run one Asterinas kernel ktest with the repository-local RISC-V OSDK binary.
# The wrapper deliberately runs from kernel/ and writes QEMU evidence outside
# target/ so a focused test does not rebuild or overwrite the whole workspace.
set -Eeuo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
    echo "usage: $0 TEST_FILTER [ARTIFACT_DIR]" >&2
    exit 2
fi

TEST_FILTER=$1
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
REPO_ROOT=$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)
KERNEL_DIR=${REPO_ROOT}/kernel
OSDK_MAIN_BIN=${OSDK_MAIN_BIN:-/home/arch-anjie/.cargo/bin/osdk-by-repo/main}
INITRAMFS=${ASTERINAS_KTEST_INITRAMFS:-${REPO_ROOT}/test/initramfs/build/initramfs.cpio.gz}
if [[ ! -f "$INITRAMFS" && -f "${REPO_ROOT}/../backups/asterinas-riscv/ktest-initramfs.cpio.gz" ]]; then
    INITRAMFS=${REPO_ROOT}/../backups/asterinas-riscv/ktest-initramfs.cpio.gz
fi

if [[ $# -eq 2 ]]; then
    ARTIFACT_DIR=$2
else
    stamp=$(date +%Y%m%d-%H%M%S)
    safe_filter=$(printf '%s' "$TEST_FILTER" | tr -c '[:alnum:]._-' '_')
    ARTIFACT_DIR=${REPO_ROOT}/../backups/asterinas-riscv-ktest/${stamp}-${safe_filter}
fi

[[ -x "$OSDK_MAIN_BIN" ]] || { echo "missing executable OSDK binary: $OSDK_MAIN_BIN" >&2; exit 2; }
[[ -f "$INITRAMFS" ]] || { echo "missing initramfs: $INITRAMFS" >&2; exit 2; }
mkdir -p "$ARTIFACT_DIR"

printf 'kernel ktest: %s\n' "$TEST_FILTER" | tee "$ARTIFACT_DIR/metadata.txt"
printf 'repo: %s\ninitramfs: %s\nosdk: %s\n' "$REPO_ROOT" "$INITRAMFS" "$OSDK_MAIN_BIN" >> "$ARTIFACT_DIR/metadata.txt"
if command -v qemu-system-riscv64 >/dev/null 2>&1; then
    qemu-system-riscv64 --version | head -n 1 >> "$ARTIFACT_DIR/metadata.txt"
fi

set +e
(
    cd "$KERNEL_DIR"
    export ASTERINAS_QEMU_LOG_DIR="$ARTIFACT_DIR"
    export OSDK_TARGET_ARCH=${OSDK_TARGET_ARCH:-riscv64}
    export VDSO_LIBRARY_DIR=${VDSO_LIBRARY_DIR:-${REPO_ROOT}/../linux_vdso}
    "$OSDK_MAIN_BIN" osdk test "$TEST_FILTER" \
        --target-arch riscv64 --scheme riscv --features riscv_sv39_mode \
        --initramfs "$INITRAMFS"
) 2>&1 | tee "$ARTIFACT_DIR/runner.log"
status=${PIPESTATUS[0]}
set -e

for log in qemu-serial.log qemu.log; do
    if [[ -f "$REPO_ROOT/$log" && ! -f "$ARTIFACT_DIR/$log" ]]; then
        cp -a "$REPO_ROOT/$log" "$ARTIFACT_DIR/$log"
    fi
done

if [[ -f "$ARTIFACT_DIR/qemu-serial.log" ]]; then
    sha256sum "$ARTIFACT_DIR"/qemu-serial.log "$ARTIFACT_DIR"/qemu.log 2>/dev/null || true
fi

# `cargo osdk test` exits 0 even when tests fail. Measured, not assumed: a run
# whose log ended `test result: FAILED. 262 passed; 6 failed` returned status 0,
# so `exit "$status"` alone reports a failing suite as a passing one. That is
# the same shape as the trap this tree already knows about one level up -- a
# run that executes nothing also exits 0 -- and it is worse here, because there
# *is* a result to read and nothing reads it.
#
# A test name containing "failed" would be a false positive, so the match is
# anchored to the harness's own summary lines.
if [[ "$status" -eq 0 ]] && grep -qE '^test result: FAILED|^test result:.*[1-9][0-9]* failed' "$ARTIFACT_DIR/runner.log" 2>/dev/null; then
    printf 'kernel ktest: the suite reported failures, but osdk exited 0:\n' >&2
    grep -E '^test result: FAILED|^test result:.*[1-9][0-9]* failed' "$ARTIFACT_DIR/runner.log" >&2
    status=1
fi

exit "$status"
