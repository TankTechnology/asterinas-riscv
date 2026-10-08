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
# 步骤：构建 marker initramfs → prepare（megrez-sv48-svade-fast）→
# run → 判定 result.json 的 classification=PASS。产物身份一并输出。

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "${REPO_ROOT}"

PROFILE="${QEMU_UBOOT_PROFILE:-megrez-sv48-svade-fast}"
OUT_DIR="${QEMU_UBOOT_OUT_DIR:-${REPO_ROOT}/target/qemu-uboot/current-megrez}"
BUILD_DIR="${QEMU_UBOOT_BUILD_DIR:-${REPO_ROOT}/target/qemu-uboot/cache/u-boot-build-megrez}"
IMAGE="${ASTERINAS_RISCV_BOOTI:-${REPO_ROOT}/target/osdk/aster-kernel/aster-kernel-osdk-bin.Image}"
INITRAMFS="${ASTERINAS_INITRAMFS:-${REPO_ROOT}/target/qemu-uboot/marker-initramfs.cpio.gz}"

echo "== [1/5] marker initramfs =="
python3 tools/riscv/make_qemu_uboot_initramfs.py "${INITRAMFS}"

echo "== [2/5] prepare (${PROFILE}) =="
ASTERINAS_RISCV_BOOTI="${IMAGE}" \
ASTERINAS_INITRAMFS="${INITRAMFS}" \
QEMU_UBOOT_PROFILE="${PROFILE}" \
QEMU_UBOOT_OUT_DIR="${OUT_DIR}" \
QEMU_UBOOT_BUILD_DIR="${BUILD_DIR}" \
tools/riscv/prepare_qemu_uboot_booti.sh prepare

echo "== [3/5] run =="
python3 tools/riscv/qemu_uboot_booti.py run \
    --profile "${PROFILE}" \
    --uboot "${BUILD_DIR}/u-boot" \
    --boot-disk "${OUT_DIR}/boot.ext4" \
    --manifest "${OUT_DIR}/artifacts.json" \
    --dtb-audit "${OUT_DIR}/qemu-dtb-audit.json" \
    --serial-log "${OUT_DIR}/serial.log" \
    --marker-event "${OUT_DIR}/marker-event.txt" \
    --result "${OUT_DIR}/result.json"

echo "== [4/5] 判定 =="
python3 - "${OUT_DIR}/result.json" "${OUT_DIR}/marker-event.txt" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
audit = result["audit"]
print("classification:", audit["classification"])
if audit["classification"] != "PASS":
    print("FAIL: Megrez 模拟未通过，禁止上板", file=sys.stderr)
    sys.exit(1)
marker = open(sys.argv[2]).read()
if "marker_seen=yes" not in marker:
    print("FAIL: 用户态 marker 未出现", file=sys.stderr)
    sys.exit(1)
print("PASS: Megrez 启动链模拟通过（用户态 marker 已见）")
PY

echo "== [5/5] 产物身份 =="
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
