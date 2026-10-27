#!/bin/bash
set -u
cd /root/asterinas
make initramfs >/dev/null 2>&1
I=$(readlink -f test/initramfs/build/initramfs.cpio.gz)
K=target/osdk/aster-kernel/aster-kernel-osdk-bin
BIN=target/riscv-sim/probe_init
mkdir -p target/riscv-sim
riscv64-linux-gnu-gcc -O2 -static -o "$BIN" tools/riscv/sim/probe_init.c -lpthread 2>&1 | tail -3
ls -l "$BIN" 2>/dev/null | head -1
rm -rf target/riscv-sim/root && mkdir -p target/riscv-sim/root
cp "$BIN" target/riscv-sim/root/init
chmod +x target/riscv-sim/root/init
(cd target/riscv-sim/root && find . | cpio -o -H newc 2>/dev/null | gzip -9) > target/riscv-sim/probe.cpio.gz
ls -l target/riscv-sim/probe.cpio.gz | head -1
timeout 90 /usr/local/qemu/bin/qemu-system-riscv64 -machine virt -m 512M -smp 2 \
  -kernel "$K" -initrd target/riscv-sim/probe.cpio.gz \
  -append "console=ttyS0" -nographic -no-reboot -monitor none \
  > target/riscv-sim/probe.log 2>&1
echo "qemu_rc=$?"
sed -E "s/\x1b\[[0-9;]*m//g" target/riscv-sim/probe.log | grep -aE "SIM_" | head -20
