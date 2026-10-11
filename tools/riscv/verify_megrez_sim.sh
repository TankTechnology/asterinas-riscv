#!/bin/bash

# SPDX-License-Identifier: MPL-2.0

# verify_megrez_sim.sh — 上板前在模拟环境复验 Megrez 启动链（一键）
#
# 在 asterinas-env:uboot-sim 容器内运行（含 qemu、riscv 交叉工具链、
# dtc、U-Boot 构建依赖）。用法：
#
#   docker run --rm -v $PWD:/root/asterinas -w /root/asterinas \
#     asterinas-env:uboot-sim bash -c 'export PATH=/usr/local/qemu/bin:$PATH
#     tools/riscv/verify_megrez_sim.sh'
#
# 步骤：构建 marker initramfs → 调用统一的 Make 运行入口 → 输出产物身份。
# 运行入口负责 prepare、QEMU 启动以及 result.json/marker 的判定。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

PROFILE="${QEMU_UBOOT_PROFILE:-megrez-sv48-svade-fast}"
OUT_DIR="${QEMU_UBOOT_OUT_DIR:-${REPO_ROOT}/target/qemu-uboot/current-megrez}"
BUILD_DIR="${QEMU_UBOOT_BUILD_DIR:-${REPO_ROOT}/target/qemu-uboot/cache/u-boot-build-megrez}"
IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image}"
INITRAMFS="${ASTERINAS_INITRAMFS:-${REPO_ROOT}/target/qemu-uboot/marker-initramfs.cpio.gz}"

echo "== [1/3] marker initramfs =="
python3 tools/riscv/make_qemu_uboot_initramfs.py "${INITRAMFS}"

echo "== [2/3] run (${PROFILE}) =="
ASTERINAS_RISCV_BOOTI="${IMAGE}" \
ASTERINAS_INITRAMFS="${INITRAMFS}" \
QEMU_UBOOT_PROFILE="${PROFILE}" \
QEMU_UBOOT_OUT_DIR="${OUT_DIR}" \
QEMU_UBOOT_BUILD_DIR="${BUILD_DIR}" \
make --no-print-directory run_riscv_uboot_booti

echo "== [3/3] 产物身份 =="
sha256sum "${IMAGE}" "${INITRAMFS}"
python3 - "${OUT_DIR}/artifacts.json" <<'PY'
import json, sys
raw = json.load(open(sys.argv[1]))
artifacts = raw.get("artifacts", raw)
for key in ("kernel_sha256", "kernel_crc32", "initrd_sha256", "initrd_crc32",
            "dtb_sha256", "dtb_crc32"):
    if key in artifacts:
        print(f"{key}: {artifacts[key]}")
PY

echo "== 复验完成：可以上板 =="
