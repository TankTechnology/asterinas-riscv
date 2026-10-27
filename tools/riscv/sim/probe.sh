#!/bin/bash
set -u
cd /root/asterinas
make initramfs >/dev/null 2>&1
I=$(readlink -f test/initramfs/build/initramfs.cpio.gz)
K=target/osdk/aster-kernel/aster-kernel-osdk-bin
OUT=${OUT:-/root/asterinas/target/riscv-sim/serial.log}
mkdir -p "$(dirname "$OUT")"; rm -f "$OUT"
timeout 120 /usr/local/qemu/bin/qemu-system-riscv64 -machine virt -m 512M -smp 2 \
  -kernel "$K" -initrd "$I" -append "console=ttyS0 loglevel=off" \
  -nographic -no-reboot -monitor none \
  < <(sleep 45; printf "\nuname -a\n"; sleep 4; printf "cat /proc/self/status | head -5\n"; sleep 4; printf "echo PROBE_DONE\n"; sleep 60) \
  > "$OUT" 2>&1
echo "qemu_rc=$?"; echo "size=$(wc -c < "$OUT")"
sed -E "s/\x1b\[[0-9;]*m//g" "$OUT" | grep -aE "PROBE_DONE|Linux version|Name:|Pid:|VmRSS|^~ #|/ #" | head -14
