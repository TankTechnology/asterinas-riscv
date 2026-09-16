#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

# Host-side checks for the Firefox/Asterinas workflow.  This intentionally does
# not build the kernel, start QEMU, touch binfmt, or access external websites.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
cd "${REPO_ROOT}"

python3 -m unittest \
  tools.riscv.tests.test_debian_browser_web \
  tools.riscv.tests.test_debian_browser_m5_runtime_gate \
  tools.riscv.tests.test_megrez_network_fixture \
  tools.riscv.tests.test_firefox_debug_tool \
  -q

python3 -m py_compile \
  tools/riscv/firefox_debug_tool.py \
  tools/riscv/debian/rootfs/browser_web_contract.py \
  tools/riscv/debian/rootfs/browser_web_marionette_gate.py \
  tools/riscv/debian/rootfs/browser_web_qemu_gate.py \
  tools/riscv/debian/rootfs/browser_web_trust_check.py \
  tools/riscv/debian/rootfs/firefox_jit_overlay.py

bash -n \
  tools/qemu_args.sh \
  tools/riscv/qemu_live_pc_sampler.sh \
  tools/riscv/qemu_system_gdb_probe.sh \
  tools/riscv/firefox_kernel_static_check.sh \
  tools/riscv/kernel_ktest.sh \
  tools/riscv/debian/rootfs/browser_web_evidence.sh \
  tools/riscv/debian/rootfs/browser_web_firefox.sh \
  tools/riscv/debian/rootfs/firefox_gdb_probe.sh \
  tools/riscv/debian/rootfs/browser_web_timeline.sh

git diff --check

echo "FIREFOX_FAST_CHECK_PASS"
