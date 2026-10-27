#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 BASE_ROOTFS OUTPUT_ROOTFS BINARY_DIR" >&2
    exit 2
}

[[ $# -eq 3 ]] || usage
base=$1
output=$2
binary_dir=$3
[[ -f $base && -d $binary_dir ]] || {
    echo "base rootfs or binary directory is missing" >&2
    exit 1
}
command -v debugfs >/dev/null || { echo "debugfs is required" >&2; exit 1; }
command -v sha256sum >/dev/null || { echo "sha256sum is required" >&2; exit 1; }

for name in lat_syscall lat_proc lat_tcp hello; do
    binary=$binary_dir/$name
    [[ -f $binary && -x $binary ]] || {
        echo "missing executable: $binary" >&2
        exit 1
    }
    if command -v readelf >/dev/null; then
        interpreter=$(readelf -l "$binary" | sed -n 's/.*Requesting program interpreter: \([^]]*\)].*/\1/p')
        [[ $interpreter == /lib/ld-linux-riscv64-lp64d.so.1 ]] || {
            echo "$name is not patched for the Debian RISC-V interpreter" >&2
            exit 1
        }
    fi
done

mkdir -p "$(dirname "$output")"
cp --reflink=auto "$base" "$output"
for name in lat_syscall lat_proc lat_tcp hello; do
    debugfs -w -R "write $binary_dir/$name /tmp/$name" "$output" >/dev/null
    debugfs -w -R "set_inode_field /tmp/$name mode 0100755" "$output" >/dev/null
done

sha256sum "$binary_dir"/{lat_syscall,lat_proc,lat_tcp,hello} >"$output.lmbench-binaries.sha256"
sha256sum "$output" >"$output.sha256"
printf '%s\n' "prepared Debian LMBench rootfs: $output"
