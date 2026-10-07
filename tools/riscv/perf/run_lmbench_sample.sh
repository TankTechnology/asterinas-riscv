#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

usage() {
    echo "usage: $0 CONTAINER SYSTEM KERNEL INITRAMFS ROOTFS [LABEL]" >&2
    exit 2
}

[[ $# -ge 5 && $# -le 6 && ( $2 == asterinas || $2 == linux ) ]] || usage
container=$1
system=$2
kernel=$3
initramfs=$4
rootfs=$5
label=${6:-Simple syscall}
log=$(mktemp)
trap 'rm -f "$log"' EXIT

append='console=ttyS0 earlycon=sbi loglevel=4 mitigations=off'
if [[ $system == asterinas ]]; then
    append="$append init=/init --"
fi
if ! docker exec "$container" timeout 120 /usr/local/qemu/bin/qemu-system-riscv64 \
    -machine virt \
    -cpu rv64,svpbmt=true,zkr=true \
    -m 8G -smp 4 -nographic -no-reboot \
    -kernel "$kernel" -initrd "$initramfs" \
    -append "$append" \
    -drive "file=$rootfs,if=none,format=raw,id=drive0" \
    -device virtio-blk-device,drive=drive0 \
    -netdev user,id=net0 \
    -device virtio-net-device,netdev=net0,csum=off,guest_csum=off,mrg_rxbuf=off,host_tso4=off,guest_tso4=off \
    >"$log" 2>&1; then
    cat "$log"
    exit 1
fi
cat "$log"
sample=$(awk -v label="$label" 'index($0, label) == 1 { for (i = 1; i < NF; i++) if ($(i + 1) ~ /^microseconds/) { print $i; exit } }' "$log")
[[ -n $sample ]] || { echo "missing PERF_SAMPLE marker" >&2; exit 1; }
printf 'PERF_SAMPLE=%s\n' "$sample"
