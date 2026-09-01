#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

# Target-architecture compile and lint gate.  This deliberately stops before
# OSDK test: it never launches QEMU and never changes host binfmt state.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
cd "${REPO_ROOT}"

readonly TARGET_ARCH="${OSDK_TARGET_ARCH:-riscv64}"
readonly FEATURES="${KERNEL_FEATURES:-riscv_sv39_mode}"
readonly OSDK_MAIN_BIN="${OSDK_MAIN_BIN:-/home/arch-anjie/.cargo/bin/osdk-by-repo/main}"
: "${VDSO_LIBRARY_DIR:?VDSO_LIBRARY_DIR must point to the matching VDSO directory}"
[[ -s "${VDSO_LIBRARY_DIR}/vdso_riscv64.so" ]] || {
  echo "missing ${VDSO_LIBRARY_DIR}/vdso_riscv64.so" >&2
  exit 2
}
[[ -x "${OSDK_MAIN_BIN}" ]] || {
  echo "missing executable OSDK binary: ${OSDK_MAIN_BIN}" >&2
  exit 2
}

OSDK_TARGET_ARCH="${TARGET_ARCH}" \
  VDSO_LIBRARY_DIR="${VDSO_LIBRARY_DIR}" \
  "${OSDK_MAIN_BIN}" osdk check --ktests -p aster-kernel \
    --features "${FEATURES}" --message-format=short

OSDK_TARGET_ARCH="${TARGET_ARCH}" \
  VDSO_LIBRARY_DIR="${VDSO_LIBRARY_DIR}" \
  "${OSDK_MAIN_BIN}" osdk clippy --ktests -p aster-kernel \
    --features "${FEATURES}" --message-format=short

git diff --check
echo "FIREFOX_KERNEL_STATIC_CHECK_PASS target=${TARGET_ARCH} features=${FEATURES}"
