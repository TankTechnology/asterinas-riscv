#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 LMBENCH_SOURCE DEBIAN_SYSROOT OUTPUT_DIR" >&2
    exit 2
}

[[ $# -eq 3 ]] || usage
source_root=$1
sysroot=$2
output=$3
src=$source_root/src
libdir=$sysroot/usr/lib/riscv64-linux-gnu
incdir=$sysroot/usr/include/tirpc
cc=${CC:-riscv64-linux-gnu-gcc}
[[ -d $src && -d $libdir && -d $incdir ]] || {
    echo "source or Debian RISC-V sysroot directories are missing" >&2
    exit 1
}
command -v "$cc" >/dev/null || { echo "cross compiler is required: $cc" >&2; exit 1; }
mkdir -p "$output"

common=(
    -O2 -std=gnu89 -I"$src" -I"$incdir"
    -L"$libdir" -Wl,-rpath-link,"$libdir"
    -Wl,-rpath,/lib:/usr/lib/riscv64-linux-gnu
    -Wl,-l:libtirpc.so.3.0.0
    -Wl,-l:libgssapi_krb5.so.2 -Wl,-l:libkrb5.so.3
    -Wl,-l:libk5crypto.so.3 -Wl,-l:libcom_err.so.2
    -Wl,-l:libkeyutils.so.1 -Wl,-l:libkrb5support.so.0 -lm
)

build() {
    local name=$1
    shift
    "$cc" "${common[@]}" -o "$output/$name" "$@"
    chmod 0755 "$output/$name"
}

build hello "$src/hello.c"
build lat_syscall "$src/lat_syscall.c" "$src/lib_timing.c" \
    "$src/lib_stats.c" "$src/lib_debug.c" "$src/lib_sched.c" "$src/getopt.c"
build lat_proc "$src/lat_proc.c" "$src/lib_timing.c" "$src/lib_stats.c" \
    "$src/lib_debug.c" "$src/lib_sched.c" "$src/getopt.c"
build lat_tcp "$src/lat_tcp.c" "$src/lib_tcp.c" "$src/lib_timing.c" \
    "$src/lib_stats.c" "$src/lib_debug.c" "$src/lib_sched.c" "$src/getopt.c"

sha256sum "$output"/{lat_syscall,lat_proc,lat_tcp,hello} >"$output/SHA256SUMS"
printf '%s\n' "built Debian-compatible LMBench binaries in $output"
