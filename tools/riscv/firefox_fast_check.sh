#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

set -euo pipefail

# Focused E1-E5 host regression. It excludes retired M5 network/browser
# workloads, physical HDMI interaction, and long QEMU runners.
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
REPO_ROOT="$(cd -- "${SCRIPT_DIR}/../.." && pwd -P)"
cd "${REPO_ROOT}"

python3 -W error::ResourceWarning -m unittest \
  tools.riscv.tests.test_debian_dev_overlay \
  tools.riscv.tests.test_debian_rootfs \
  tools.riscv.tests.test_debian_desktop_m4_drm_recovery_gate \
  tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_megrez_control_chain \
  tools.riscv.tests.test_megrez_display_gate \
  tools.riscv.tests.test_megrez_recovery_epoch \
  tools.riscv.tests.test_qemu_scanout_contract \
  -q

python3 -m py_compile \
  tools/riscv/refactor_reference_scan.py \
  tools/riscv/debian/rootfs/gate_protocol.py \
  tools/riscv/debian/rootfs/gate_runtime.py \
  tools/riscv/debian/rootfs/rootfs_gate.py \
  tools/riscv/debian/rootfs/desktop_m4_gate.py \
  tools/riscv/debian/rootfs/desktop_m4_recovery_gate.py \
  tools/riscv/debian/rootfs/firefox_drm_scene_gate.py \
  tools/riscv/megrez_control_chain.py \
  tools/riscv/megrez_display_gate.py

bash -n \
  tools/riscv/debian/rootfs/build_rootfs.sh \
  tools/riscv/debian/rootfs/build_stage1.sh

git diff --check
echo "FIREFOX_FAST_CHECK_PASS"
