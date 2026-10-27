#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 CONTAINER KERNEL INITRAMFS [LABEL]" >&2
    exit 2
}

[[ $# -ge 3 && $# -le 4 ]] || usage
container=$1
kernel=$2
initramfs=$3
label=${4:-Simple syscall}

log=$(mktemp)
trap 'rm -f "$log"' EXIT
if ! docker exec "$container" timeout 90 /usr/local/qemu/bin/qemu-system-riscv64 \
    -machine virt \
    -cpu rv64,svpbmt=true,zkr=true \
    -m 8G -smp 4 -nographic -no-reboot \
    -kernel "$kernel" -initrd "$initramfs" \
    -append 'console=ttyS0 earlycon=sbi loglevel=4 mitigations=off' >"$log" 2>&1; then
    cat "$log"
    exit 1
fi
cat "$log"

sample=$(awk -v label="$label" 'index($0, label ":") == 1 { print $3; exit }' "$log")
[[ -n $sample ]] || { echo "missing Simple syscall sample" >&2; exit 1; }
printf 'PERF_SAMPLE=%s\n' "$sample"
