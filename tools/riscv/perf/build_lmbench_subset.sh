#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Build the LMBench subset used by bench_gate.py as static riscv64-musl binaries.
#
# LMBench is pinned to the revision the repository's native workflow uses
# (afb47edd...). The tree is old C: compile with -std=gnu89 and pull in
# string.h/stdlib.h/unistd.h that it assumes implicitly. The RPC headers are
# stubbed because the selected subset never uses RPC, while bench.h includes
# them unconditionally.
set -euo pipefail
REPO_ROOT="$(cd "${BASH_SOURCE[0]%/*}/../../.." && pwd)"
SRC="${REPO_ROOT}/target/perf/lmbench"
OUT="${REPO_ROOT}/target/perf/lmbench-subset"
STUB="${REPO_ROOT}/target/perf/lmbench-stub"
REV=afb47eddaf10a411c1ea3cb64965461f1308a6ea

if [ ! -d "${SRC}/src" ]; then
    git clone https://github.com/intel/lmbench.git "${SRC}"
fi
git -C "${SRC}" checkout "${REV}"

mkdir -p "${OUT}" "${STUB}/rpc"
: > "${STUB}/rpc/rpc.h"
: > "${STUB}/rpc/types.h"

CC=(riscv64-linux-musl-gcc -std=gnu89 -static -include string.h -include stdlib.h
    -include unistd.h -I"${STUB}" -I"${SRC}/src" -L/usr/riscv64-linux-gnu/lib)

# The archive is built once by the upstream Makefile (it compiles lib_tcp.o etc.
# and fails later only when linking network tests against tirpc).
( cd "${SRC}/src" && make OS=riscv64-unknown-linux-gnu \
    CC="riscv64-linux-musl-gcc -std=gnu89 -static -include string.h -include stdlib.h -include unistd.h -L/usr/riscv64-linux-gnu/lib" \
    || true )
LIB="${SRC}/bin/riscv64-unknown-linux-gnu/lmbench.a"
[ -f "${LIB}" ] || { echo "missing ${LIB}: upstream library build failed" >&2; exit 1; }

for test in lat_syscall lat_pipe lat_proc lat_ctx lat_mmap lat_sig lat_fs \
            lat_pagefault bw_pipe bw_mem bw_mmap_rd lat_ops lat_fcntl; do
    "${CC[@]}" -O2 -o "${OUT}/${test}" "${SRC}/src/${test}.c" "${LIB}" -lm
done

riscv64-linux-gnu-strip "${OUT}"/* 2>/dev/null || true
echo "built ${OUT}"
