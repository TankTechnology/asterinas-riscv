#!/bin/bash
set -u
cd /root/asterinas/test/initramfs
PROBE=$(nix-build nix/default.nix --argstr target riscv64 -A simProbe --no-out-link 2>/dev/null | tail -1)
echo "probe=$PROBE"
ls -l "$PROBE/bin/sim-probe" | head -1
cd /root/asterinas
rm -rf target/riscv-sim/root && mkdir -p target/riscv-sim/root
cp "$PROBE/bin/sim-probe" target/riscv-sim/root/init
chmod +x target/riscv-sim/root/init
(cd target/riscv-sim/root && find . | cpio -o -H newc 2>/dev/null | gzip -9) > target/riscv-sim/probe.cpio.gz
ls -l target/riscv-sim/probe.cpio.gz | head -1
timeout 70 /usr/local/qemu/bin/qemu-system-riscv64 -machine virt -m 512M -smp 2 \
  -kernel target/osdk/aster-kernel/aster-kernel-osdk-bin \
  -initrd target/riscv-sim/probe.cpio.gz \
  -append "console=ttyS0" -nographic -no-reboot -monitor none \
  > target/riscv-sim/probe.log 2>&1
echo "qemu_rc=$?"
sed -E "s/\x1b\[[0-9;]*m//g" target/riscv-sim/probe.log | grep -aE "SIM_" | head -20
