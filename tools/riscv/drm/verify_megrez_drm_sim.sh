#!/usr/bin/env bash
# SPDX-License-Identifier: MPL-2.0

# verify_megrez_drm_sim.sh — 上板前在模拟环境复验 Megrez 的 DRM 固件扫描输出
#
# `verify_megrez_sim.sh` 验的是启动链：用户态 marker 出现即算通过。它跑在
# headless 设备集上，对显示一无所知。这个脚本验的是显示——在没有 GPU 的机器
# 上，固件 framebuffer 后端能不能经 SETCRTC / PAGE_FLIP / DIRTYFB 三条路径
# 把正确的像素送上屏。
#
# 为什么不能拿现成的 DRM gate 结果顶上：那些 gate 都跑在 `qemu-virt` 的 Sv39
# 契约上（`-cpu ...,sv48=false`），而板子跑的是 **Sv48** 内核——`OSDK.toml` 的
# riscv scheme 不带 `riscv_sv39_mode`，不传 feature 就是 Sv48。内核的 VA 常量
# 由分页模式算出（`ADDRESS_WIDTH` 39/48 决定 `KERNEL_BASE_VADDR`、
# `LINEAR_MAPPING_BASE_VADDR`、`VMALLOC_BASE_VADDR`），所以"固件后端能把像素
# 拷进 framebuffer"必须在板子的分页模式下重新成立。
#
# 内核必须与机器契约配套，而**用错方向不会报错**：
#   - Sv39 内核在 Megrez 契约上**也能启动**（`sv48` 没被禁用，Sv48 蕴含
#     Sv39），所以拿错内核不会失败，只会静静地测了另一个东西，然后报 pass。
#     这正是本脚本默认自己构建内核的原因：模式无法从 Image 反查
#     （`sv39_boot_l3pt` / `sv48_boot_l4pt` 两张表都是无条件汇编进去的），
#     只能靠"用哪个 feature 构建"来保证。cargo 按 feature 缓存，已经对的时候
#     构建是秒级的。
#   - 反过来，Sv48 内核在 `sv48=false` 的通用 profile 上会挂在
#     `Starting kernel ...`，没有任何输出。
#
# 用法：
#   tools/riscv/drm/verify_megrez_drm_sim.sh
#
# 环境变量：
#   ASTERINAS_RISCV_BOOTI    内核 Image；给了就跳过构建（模式自负）
#   MEGREZ_SKIP_KERNEL_BUILD 置 1 时不构建内核，只用已有的
#   QEMU_UBOOT_OUT_DIR       产物目录
#   QEMU_UBOOT_BUILD_DIR     U-Boot 构建目录
#   MEGREZ_DRM_EVIDENCE_DIR  证据目录

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

PROFILE="megrez-sv48-svade-drm-firmware"
DEVICE_SET="megrez-basic"

# 本工作树的 `target` 是指向主检出目录的软链，而 secure I/O 拒绝穿过软链，
# 所以路径先解到真实目录再交给 python 侧。
TARGET_REAL="$(readlink -f "${REPO_ROOT}/target")"
OUT_DIR="${QEMU_UBOOT_OUT_DIR:-${TARGET_REAL}/qemu-uboot/current-megrez-drm-firmware}"
BUILD_DIR="${QEMU_UBOOT_BUILD_DIR:-${TARGET_REAL}/qemu-uboot/cache/u-boot-build-megrez}"
EVIDENCE_DIR="${MEGREZ_DRM_EVIDENCE_DIR:-${OUT_DIR}/drm-evidence}"
IMAGE="${ASTERINAS_RISCV_BOOTI:-${TARGET_REAL}/osdk/aster-kernel-osdk-bin.Image}"
INITRAMFS="${TARGET_REAL}/drm-firmware/megrez-initramfs.cpio.gz"

if [[ "${MEGREZ_SKIP_KERNEL_BUILD:-0}" != 1 && -z "${ASTERINAS_RISCV_BOOTI:-}" ]]; then
    echo "== [1/5] 构建 Sv48 内核（不传 FEATURES）=="
    # 工作树里 osdk 不是 workspace 成员，Makefile 的 install_osdk 会失败，且
    # $(CARGO_OSDK) 是文件目标，必须用**命令行赋值**覆盖——环境变量会被
    # Makefile 的立即赋值悄悄盖掉。
    make_args=()
    if [[ -x "${REPO_ROOT}/.osdk-bin/bin/cargo-osdk" ]]; then
        make_args+=("CARGO_OSDK=${REPO_ROOT}/.osdk-bin/bin/cargo-osdk")
        export PATH="${REPO_ROOT}/.osdk-bin/bin:${PATH}"
    fi
    # VDSO_LIBRARY_DIR 指向真实的那个：`~/Program/build-check/vdso` 里是 45 字节
    # 占位文件，用它构建会成功，然后内核在第一个线程上 panic。
    CARGO_NET_OFFLINE=true RELEASE=1 TARGET_ARCH=riscv64 \
        VDSO_LIBRARY_DIR="${VDSO_LIBRARY_DIR:-${HOME}/.local/share/linux_vdso}" \
        make kernel "${make_args[@]}"
else
    echo "== [1/5] 跳过内核构建，使用 ${IMAGE} =="
    echo "   警告：内核的分页模式没有被本脚本验证。板子跑 Sv48；" >&2
    echo "   若这是 Sv39 构建，本 gate 会照样通过，但测的不是板子的模式。" >&2
fi
step=2

echo "== [${step}/5] 构建固件 gate initramfs =="
bash tools/riscv/drm/build_firmware_gate.sh "${INITRAMFS}"
step=$((step + 1))

echo "== [${step}/5] prepare (${PROFILE}) =="
ASTERINAS_RISCV_BOOTI="${IMAGE}" \
ASTERINAS_INITRAMFS="${INITRAMFS}" \
QEMU_UBOOT_PROFILE="${PROFILE}" \
QEMU_UBOOT_OUT_DIR="${OUT_DIR}" \
QEMU_UBOOT_BUILD_DIR="${BUILD_DIR}" \
tools/riscv/prepare_qemu_uboot_booti.sh prepare
step=$((step + 1))

echo "== [${step}/5] 固件扫描输出 gate =="
mkdir -p "${EVIDENCE_DIR}"
PYTHONPATH="${REPO_ROOT}/tools/riscv" python3 tools/riscv/drm/firmware_gate.py \
    --uboot "${BUILD_DIR}/u-boot" \
    --boot-disk "${OUT_DIR}/boot.ext4" \
    --manifest "${OUT_DIR}/artifacts.json" \
    --dtb-audit "${OUT_DIR}/qemu-dtb-audit.json" \
    --output-directory "${EVIDENCE_DIR}" \
    --profile "${PROFILE}" \
    --device-set "${DEVICE_SET}"
step=$((step + 1))

echo "== [${step}/5] 产物身份 =="
sha256sum "${IMAGE}" "${INITRAMFS}"
python3 - "${EVIDENCE_DIR}/boot-result.json" <<'PY'
import json, sys
result = json.load(open(sys.argv[1]))
audit = result["audit"]
print("device_set:", result["device_set"])
print("kernel_size:", result["artifacts"]["kernel_size"])
print("classification:", audit["classification"])
print("effective_bootargs:", audit["effective_bootargs"])
PY

echo "== 复验完成：可以上板 =="
