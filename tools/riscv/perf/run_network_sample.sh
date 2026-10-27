#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0
set -euo pipefail

[[ $# -eq 5 && ( $2 == asterinas || $2 == linux ) ]] || {
    echo "usage: $0 CONTAINER {asterinas|linux} KERNEL INITRAMFS ROOTFS" >&2
    exit 2
}
docker exec -i "$1" bash -s _ "$2" "$3" "$4" "$5" <<'SCRIPT'
set -euo pipefail
system=$2
kernel=$3
initramfs=$4
rootfs=$5
server_log=$(mktemp)
server_pid=
cleanup() {
    if [[ -n $server_pid ]]; then
        kill "$server_pid" 2>/dev/null || true
        wait "$server_pid" 2>/dev/null || true
    fi
    cat "$server_log" >&2
    rm -f "$server_log"
}
trap cleanup EXIT
timeout 120 /root/.nix-profile/bin/iperf3 -s -B 0.0.0.0 -p 15201 >"$server_log" 2>&1 &
server_pid=$!
sleep 0.5
kill -0 "$server_pid"
append='console=ttyS0 earlycon=sbi loglevel=4 mitigations=off'
if [[ $system == asterinas ]]; then
    append="$append init=/init --"
fi
timeout 90 /usr/local/qemu/bin/qemu-system-riscv64 \
    -machine virt -cpu rv64,svpbmt=true,zkr=true -m 8G -smp 4 \
    -nographic -no-reboot -kernel "$kernel" -initrd "$initramfs" \
    -append "$append" \
    -drive "file=$rootfs,if=none,format=raw,id=drive0" \
    -device virtio-blk-device,drive=drive0 \
    -netdev user,id=net0 \
    -device virtio-net-device,netdev=net0,csum=off,guest_csum=off,mrg_rxbuf=off,host_tso4=off,guest_tso4=off
SCRIPT
