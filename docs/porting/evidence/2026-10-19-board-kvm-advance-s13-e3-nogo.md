# S13：E3 整机可行性（Phase 2）—— 板上 no-go 结论

**日期**：2026-10-19（宿主 pku4090 时钟）
**结论**：**板上当前不具备 E3 所需的 GL/virgl 渲染路径**；按 Lead 的判定规则，**Phase 2 停止**，不搬运/不运行 E3 门禁。仅软件渲染（llvmpipe）可用，与"renderer 非 llvmpipe"的验收冲突。
**板子状态**：未重启、未改 boot、未持久化；kvm 模块仍加载、`/dev/kvm` 可用（`crw-rw-rw-`，S5 临时 chmod）；无 QEMU 进程；`boot_id 9c777a00-…` 未变。全程只读检查 + 一次 10 s 有界 QEMU 初始化尝试（未改任何文件）。

---

## 1. 能力检查结果（全部在板上实测）

### 1.1 QEMU（9.2.0，Debian 1:9.2.0+ds-5rockos1）

- `qemu-system-riscv64 -device help | grep -i virtio-gpu`：
  ```
  virtio-gpu-device / virtio-gpu-gl-device / virtio-gpu-gl-pci / virtio-gpu-pci / virtio-vga / virtio-vga-gl
  ```
- `qemu-system-riscv64 -display help`：`none gtk sdl egl-headless curses dbus` → 存在 `egl-headless`。
- `libvirglrenderer.so.1.8.8` 存在（`/usr/lib/riscv64-linux-gnu/`）；Mesa EGL/GBM/libGL/libGLESv2 均在。
- 结论：**QEMU 二进制本身支持 virgl（virtio-gpu-gl / egl-headless）**。

### 1.2 宿主 GPU/DRM（决定性）

- `/dev/dri/` 只有 `card0`（`crw-rw----+ root video 226,0`，显示控制器），**没有 `/dev/dri/renderD*`**。
- 有界初始化测试（10 s）：
  ```
  qemu-system-riscv64 -machine virt -accel kvm -cpu host -m 512 -smp 1 \
    -display egl-headless -device virtio-gpu-gl-pci -bios none -S -monitor none -serial none
  → qemu-system-riscv64: egl: no drm render node available
  → qemu-system-riscv64: egl: render node init failed
  ```
  `LIBGL_ALWAYS_SOFTWARE=1` 重试结果相同。
- `lsmod | grep -iE 'powervr|pvr|drm|gpu'`：无输出（无 GPU 渲染内核模块；`card0` 为显示用途）。
- Mesa 有 `swrast_dri.so` / `kms_swrast_dri.so`（llvmpipe 软件渲染兜底），**但 egl-headless 需要 DRM render node，软件渲染也不能让 virgl host 起来**（QEMU 报错即证）。

### 1.3 资源

| 项 | 值 |
| --- | --- |
| `/` | 113G，可用 36G（68%） |
| `/boot` | 459M，可用 16M（97%，不宜再放东西） |
| `/tmp` | tmpfs 4.9G，可用 4.7G |
| RAM | 9.7 GiB，可用约 9.0 GiB |
| KVM | `/dev/kvm` 存在且 `debian` 可读写（S5 临时 chmod 666；非持久化） |

## 2. 判定

| Lead 给定判据 | 实测 | 结果 |
| --- | --- | --- |
| 板上 QEMU 是否支持 virtio-gpu GL | 支持（含 `virtio-gpu-gl-pci`、`egl-headless`、libvirglrenderer） | ✅ |
| 是否存在可用 GL/virgl host 路径 | **否**：无 DRM render node，`egl-headless` 初始化失败 | ❌ |
| 无 GL 时 → 停止 Phase 2 | — | **按规则停止** |

**理由**：E3 要求 renderer 非 llvmpipe（硬件/虚拟 GL），而板上唯一可行路径是 llvmpipe 软件渲染 → 即使把 E3 资产搬上来跑，也只会得到"软件渲染"结果，与验收目标冲突，且会消耗大量传输/运行时间。

## 3. 建议（独立项目，供 Lead 决策）

1. **板上自建带 virgl 的 QEMU + GPU 渲染栈**（独立项目）：
   - 使能板载 GPU（EIC7700/P550 平台的 GPU 驱动，如 PowerVR 系列）并产出 `/dev/dri/renderD*`；
   - 或明确接受"仅 llvmpipe"，并**单独立项**修订 E3 验收标准（不与"renderer 非 llvmpipe"混用）。
2. **E3 在开发机/容器（x86 或已有 GL 的宿主）上执行**，板载 KVM 用于 CPU/整机吞吐类验证（S6/S8 口径）。
3. 若未来板上有 render node，可按 s13 的同一 `qemu -display egl-headless -device virtio-gpu-gl-pci -accel kvm` 冒烟作为第一步（≤10 s）再搬运 E3 资产。

## 4. 未做（按规则）

- 未从仓库取 E3 门禁脚本 / 官方 Debian rootfs / Firefox deb（避免在 no-go 前做数百 MB 级传输）；
- 未在板上跑 E3 尝试（≤600 s 的那一次未启动）；
- 未改任何板侧配置；未动主工作区。

---

## 5. 板上 KVM 的定位（收口结论）

**板载 KVM = 功能 / 并发 / 性能验证环境（不含 GPU 加速）**；需要硬件/虚拟 GL 的 **E3 加速版继续留在 pku4090（开发宿主）执行**。板上 E3 加速版不立项，除非先解决下述"救活条件"。

### 5.1 最小复现命令（板上，只读/有界，约 10 s）

```bash
# 1) 确认 QEMU 有 GL 设备（能编进来）
qemu-system-riscv64 -device help 2>&1 | grep -iE 'virtio-gpu|virgl'
qemu-system-riscv64 -display help 2>&1 | grep -i egl-headless

# 2) 确认宿主有没有 DRM render node（决定 virgl 能否起来）
ls -l /dev/dri/                  # 只有 card0，没有 renderD*  → 不可用
ls /usr/lib/riscv64-linux-gnu/libvirglrenderer.so*   # 库存在

# 3) 一次有界初始化（会直接给出结论）
timeout 10 qemu-system-riscv64 -machine virt -accel kvm -cpu host -m 512 -smp 1 \
  -display egl-headless -device virtio-gpu-gl-pci -bios none -S -monitor none -serial none
# 实测输出：
#   qemu-system-riscv64: egl: no drm render node available
#   qemu-system-riscv64: egl: render node init failed
```

### 5.2 若要"救活"板上 E3，需要什么（按依赖顺序）

1. **算力侧（已有）**：`/dev/kvm` + `qemu-system-riscv64`（S12 已做成开机自动加载，`-accel kvm` 可用）。
2. **GPU 内核侧（缺）**：板载 GPU（EIC7700/P550 平台的 GPU，如 Imagination PowerVR 系列）的 DRM 驱动需使能，产出 **`/dev/dri/renderD*`** 渲染节点；当前只有 `card0`（显示控制器）且 `lsmod` 无 GPU 渲染模块。
3. **用户态 GL 栈（部分存在）**：Mesa（EGL/GBM/libGL/GLESv2）+ `libvirglrenderer.so.1.8.8` 已在板上；但需要 (2) 的 render node 才能让 `egl-headless`/virgl host 初始化。
4. **QEMU 侧（已有）**：本板 QEMU 9.2.0 已带 `virtio-gpu-gl-device`/`virtio-gpu-gl-pci`/`virtio-vga-gl` 与 `egl-headless`，无需重编。
5. **验收**：起 `-device virtio-gpu-gl-pci -display egl-headless` 后，客户内 renderer 报 **非 llvmpipe**（如 `virgl`），才算满足 E3 原验收；否则只能按"软件渲染"另立验收标准。

> 结论：这是一条**独立的板上 GPU/DRM 渲染栈项目**（驱动 + render node），不是本次 KVM 任务能顺手完成的；在其可用之前，板上 E3 不跑。
