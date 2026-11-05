#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Status: manual -- static compile/lint gate; also py_compiled by firefox_fast_check.sh. See tools/riscv/LAYOUT.md.

set -euo pipefail

# Target-architecture compile and lint gate.  This deliberately stops before
# OSDK test: it never launches QEMU and never changes host binfmt state.
# shellcheck source=../lib/shell.sh
_shell_self_dir=${BASH_SOURCE[0]%/*}
if [[ ${_shell_self_dir} == "${BASH_SOURCE[0]}" ]]; then _shell_self_dir=.; fi
source "${_shell_self_dir}/../lib/shell.sh"
unset _shell_self_dir
REPO_ROOT="$(repo_root)"
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
