#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
# Cross-build the static riscv64-musl fio used by fio_gate.py.
set -euo pipefail
REPO_ROOT="$(cd "${BASH_SOURCE[0]%/*}/../../.." && pwd)"
SRC="${REPO_ROOT}/target/perf/fio-src"
OUT="${REPO_ROOT}/target/perf/fio-bin"
if [ ! -d "${SRC}" ]; then
    git clone --depth 1 https://github.com/axboe/fio.git "${SRC}"
fi
cd "${SRC}"
export LDFLAGS="-L/usr/riscv64-linux-gnu/lib"
./configure --cc=riscv64-linux-musl-gcc --build-static --cpu=riscv64 --disable-native \
    --disable-shm --disable-numa --disable-rdma --disable-rados --disable-rbd \
    --disable-http --disable-gfapi --disable-libnfs --disable-pmem --disable-libzbc \
    --disable-isal --disable-libblkio --disable-tcmalloc --disable-dfs \
    --extra-cflags="-L/usr/riscv64-linux-gnu/lib -idirafter /usr/riscv64-linux-gnu/include"
make -j"$(nproc)"
mkdir -p "${OUT}"
cp fio "${OUT}/fio"
riscv64-linux-gnu-strip "${OUT}/fio" 2>/dev/null || true
echo "built ${OUT}/fio"
