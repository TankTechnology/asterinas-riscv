#!/bin/bash
set -u
cd /root/asterinas
timeout 240 make run_kernel TARGET_ARCH=riscv64 > target/riscv-sim/probe4.log 2>&1
echo "rc=$?"
sed -E "s/\x1b\[[0-9;]*m//g" target/riscv-sim/probe4.log | grep -aE "SIM_" | head -20
echo "--- tail:"
sed -E "s/\x1b\[[0-9;]*m//g" target/riscv-sim/probe4.log | tail -4 | cut -c1-100
