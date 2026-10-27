#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 CONTAINER KERNEL INITRAMFS ROOTFS" >&2
    exit 2
}

[[ $# -eq 4 ]] || usage
container=$1
kernel=$2
initramfs=$3
rootfs=$4
log=$(mktemp)
trap 'rm -f "$log"' EXIT

if ! docker exec "$container" timeout 120 /usr/local/qemu/bin/qemu-system-riscv64 \
    -machine virt \
    -cpu rv64,svpbmt=true,zkr=true \
    -m 8G -smp 4 -nographic -no-reboot \
    -kernel "$kernel" -initrd "$initramfs" \
    -append 'console=ttyS0 earlycon=sbi loglevel=4 mitigations=off init=/init --' \
    -drive "file=$rootfs,if=none,format=raw,id=drive0" \
    -device virtio-blk-device,drive=drive0 >"$log" 2>&1; then
    cat "$log"
    exit 1
fi
cat "$log"
sample=$(awk -F= '/^PERF_SAMPLE=/ { print $2; exit }' "$log")
if [[ -z $sample ]] && grep -q 'Request Latencies' "$log"; then
    sample=$(awk '/99\.0th:/ { value=$3 } END { if (value != "") print value }' "$log")
    [[ -n $sample ]] && printf 'PERF_SAMPLE=%s\n' "$sample"
fi
[[ -n $sample ]] || { echo "missing PERF_SAMPLE marker" >&2; exit 1; }
