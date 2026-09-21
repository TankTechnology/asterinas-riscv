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
# **跑两组设备集，各自成立一个不同的主张**（见 qemu_uboot_devices.py）：
#
#   megrez-basic          0x40000000, 1280x1024 —— 在**内存之外**，QEMU
#                         monitor 截得到真图，所以截图是独立证据；但
#                         "从分配器里把 scanout 挖掉"这条路是空的。
#   megrez-board-geometry 0x90000000, 1920x1080 stride 7680 —— **板子的几何**，
#                         且落在 DRAM **内部**，走的正是板子那条路；代价是
#                         bochs 的 BAR 在别处、没人写它，截图必然全黑，这时
#                         判据只有探针自己的 fbdev 回读。
#
# 两组都过，才谈得上"板子的几何、以及 DRAM 内预留这两件事都验过"。缺任何
# 一组，都还剩一个只在板子上才成立的假设。
#
# **板子的真实地址 0xfd800000 在 2GiB 上无法模拟**，这一点写在
# `MEGREZ_BOARD_GEOMETRY` 的注释里：U-Boot 自己的栈就在那段（`[0xfde96000,
# 0xffffffff]`），和 scanout `[0xfd800000, 0xfdfe9000)` 重叠。U-Boot 会把设备树
# 搬进 scanout 里，内核启动后写屏、把自己启动用的那棵树覆盖掉，91ms 就
# panic（`/cpus is a required node`）。声明 `/reserved-memory` 也救不了——
# U-Boot 的 `boot_fdt_reserve_region()` 会把与自身重叠导致的 `-EEXIST`
# 静默吞掉。板子有 16GiB，U-Boot 在 0x4_8000_0000 附近，不受影响。
#
# 用法：
#   tools/riscv/drm/verify_megrez_drm_sim.sh
#
# 环境变量：
#   ASTERINAS_RISCV_BOOTI    内核 Image；给了就跳过构建（模式自负）
#   MEGREZ_SKIP_KERNEL_BUILD 置 1 时不构建内核，只用已有的
#   QEMU_UBOOT_OUT_DIR       产物目录（每组设备集各自加后缀）
#   QEMU_UBOOT_BUILD_DIR     U-Boot 构建目录
#   MEGREZ_DRM_EVIDENCE_DIR  证据目录（每组设备集各自加后缀）

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../../.." && pwd)"
cd "${REPO_ROOT}"

PROFILE="megrez-sv48-svade-drm-firmware"
DEVICE_SETS=("megrez-basic" "megrez-board-geometry")

# 本工作树的 `target` 是指向主检出目录的软链，而 secure I/O 拒绝穿过软链，
# 所以路径先解到真实目录再交给 python 侧。
TARGET_REAL="$(readlink -f "${REPO_ROOT}/target")"
OUT_DIR="${QEMU_UBOOT_OUT_DIR:-${TARGET_REAL}/qemu-uboot/current-megrez-drm-firmware}"
BUILD_DIR="${QEMU_UBOOT_BUILD_DIR:-${TARGET_REAL}/qemu-uboot/cache/u-boot-build-megrez}"
EVIDENCE_ROOT="${MEGREZ_DRM_EVIDENCE_DIR:-${OUT_DIR}/drm-evidence}"
IMAGE="${ASTERINAS_RISCV_BOOTI:-${TARGET_REAL}/osdk/aster-kernel-osdk-bin.Image}"

if [[ "${MEGREZ_SKIP_KERNEL_BUILD:-0}" != 1 && -z "${ASTERINAS_RISCV_BOOTI:-}" ]]; then
    echo "== 构建 Sv48 内核（不传 FEATURES）=="
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
    echo "== 跳过内核构建，使用 ${IMAGE} =="
    echo "   警告：内核的分页模式没有被本脚本验证。板子跑 Sv48；" >&2
    echo "   若这是 Sv39 构建，本 gate 会照样通过，但测的不是板子的模式。" >&2
fi

# 探针把期望的几何编译进去，那些数字必须来自**写出 DTB 节点的那份契约**，
# 否则期望和节点会各自漂移，而两边都由同一个脚本提供时这种漂移不会报错。
#
# 几何同时是 **U-Boot 的构建输入**：QEMU 的 bochs 屏幕尺寸由 U-Boot 编译期
# 的 `CONFIG_VIDEO_BOCHS_SIZE_X/Y` 决定（`pci display 0.1.0` 让 U-Boot 自己
# 编程设备，把 QEMU 命令行的 `xres=`/`yres=` 覆盖掉），而 runner 会拒绝尺寸
# 与契约不符的截图。所以每个几何各用一份 U-Boot 构建目录，见下面循环。
framebuffer_geometry() {
    PYTHONPATH="${REPO_ROOT}/tools/riscv" python3 - "$1" <<'PY'
import sys

from qemu_uboot_devices import device_set_by_name

framebuffer = device_set_by_name(sys.argv[1]).framebuffer
if framebuffer is None:
    raise SystemExit(f"device set {sys.argv[1]} declares no framebuffer")
print(framebuffer.width, framebuffer.height, framebuffer.stride)
PY
}

build_gate_initramfs() {
    local device_set="$1" output="$2"
    local width height stride
    read -r width height stride < <(framebuffer_geometry "${device_set}")
    GATE_MODE_WIDTH="${width}" GATE_MODE_HEIGHT="${height}" \
        GATE_MODE_STRIDE="${stride}" \
        bash tools/riscv/drm/build_firmware_gate.sh "${output}"
}

for device_set in "${DEVICE_SETS[@]}"; do
    set_out="${OUT_DIR}/${device_set}"
    set_evidence="${EVIDENCE_ROOT}/${device_set}"
    initramfs="${TARGET_REAL}/drm-firmware/${device_set}.cpio.gz"

    # QEMU's bochs surface is whatever U-Boot was compiled to program, and the
    # runner refuses a capture whose dimensions differ from the contract. So
    # the U-Boot build is a per-geometry input, and each geometry needs its own
    # build directory -- the two configurations cannot share one.
    read -r fw_width fw_height fw_stride < <(framebuffer_geometry "${device_set}")
    set_build_dir="${BUILD_DIR}-${fw_width}x${fw_height}"

    echo "== prepare (${PROFILE} + ${device_set}, ${fw_width}x${fw_height}) =="
    build_gate_initramfs "${device_set}" "${initramfs}"
    ASTERINAS_RISCV_BOOTI="${IMAGE}" \
    ASTERINAS_INITRAMFS="${initramfs}" \
    QEMU_UBOOT_PROFILE="${PROFILE}" \
    QEMU_UBOOT_OUT_DIR="${set_out}" \
    QEMU_UBOOT_BUILD_DIR="${set_build_dir}" \
    QEMU_UBOOT_BOCHS_SIZE="${fw_width}x${fw_height}" \
    tools/riscv/prepare_qemu_uboot_booti.sh prepare

    echo "== gate (${device_set}) =="
    mkdir -p "${set_evidence}"
    PYTHONPATH="${REPO_ROOT}/tools/riscv" python3 tools/riscv/drm/firmware_gate.py \
        --uboot "${set_build_dir}/u-boot" \
        --boot-disk "${set_out}/boot.ext4" \
        --manifest "${set_out}/artifacts.json" \
        --dtb-audit "${set_out}/qemu-dtb-audit.json" \
        --output-directory "${set_evidence}" \
        --profile "${PROFILE}" \
        --device-set "${device_set}"
done

echo "== 产物身份 =="
sha256sum "${IMAGE}"
python3 - "${EVIDENCE_ROOT}" "${DEVICE_SETS[@]}" <<'PY'
import json
import sys
from pathlib import Path

root, *device_sets = sys.argv[1:]
for device_set in device_sets:
    result = json.loads((Path(root) / device_set / "boot-result.json").read_text())
    audit = result["audit"]
    print(
        f"{device_set}: device_set={result['device_set']} "
        f"kernel_size={result['artifacts']['kernel_size']} "
        f"classification={audit['classification']} "
        f"bootargs={audit['effective_bootargs']!r}"
    )
PY

echo "== 复验完成：可以上板 =="
