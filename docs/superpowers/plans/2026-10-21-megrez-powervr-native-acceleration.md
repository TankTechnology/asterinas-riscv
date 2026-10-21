# Megrez PowerVR 原生硬件加速移植计划（技术拆解与分阶段）

日期：2026-10-21
状态：计划（无实现代码）；参考栈已在 RockOS 验证通过
关联：

- 参考栈收据：[2026-10-21-rockos-powervr-reference.md](../../porting/evidence/2026-10-21-rockos-powervr-reference.md)
- 既有六阶段加速计划：[2026-10-14-desktop-gpu-acceleration.md](2026-10-14-desktop-gpu-acceleration.md)
- 存量状态：[2026-10-14-desktop-gpu-acceleration-status.md](2026-10-14-desktop-gpu-acceleration-status.md)
- 板载 KVM no-go：[2026-10-19-board-kvm-advance-s13-e3-nogo.md](../../porting/evidence/2026-10-19-board-kvm-advance-s13-e3-nogo.md)

## 1. 结论摘要

1. **硬件与厂商栈可用**：用与模块同源的 `6.6.87-win2030` 内核引导
   RockOS 后，`pvrsrvkm` 加载 `rgx.fw/sh.30.3.408.101`，出现
   `card1`/`renderD128`，Xorg glamor 在
   `PowerVR A-Series AXM-8-256` 上启用。参考栈不是瓶颈。
2. **Asterinas 侧缺口是驱动**：当前只有 opt-in 探测（3.8k 行 Rust），
   停在"META 保持复位、未释放"（`powervr_probe/power.rs:723`），
   自报 `firmware=untouched render=unavailable`（`power.rs:926`）；
   DRM 子系统也只有单设备 `card0`/`renderD128`
   （`kernel/src/device/dri.rs:5,213-214`），内核组件没有 C/FFI 构建机制，
   也没有固件加载设施。
3. **上游不能替代**：`drm/imagination` 当前列出的 BVNC 是 AXE-1-16M
   `33.15.11.3` 与 BXS-4-64 `36.53.104.796`；Megrez 是 AXM-8-256
   `30.3.408.101`。Mesa `powervr` 只对接上游 UAPI，不能驱动厂商
   `pvrsrvkm`。
4. **推荐架构**：**MIT 许可的厂商 C 核心 + Rust OS/DRM 层**（方案 A）。
   厂商源码是 Dual MIT/GPLv2（`pvr_drm.c` 文件头），可选 MIT 分支；
   需要一次 framekernel 政策决策（是否允许受控 C 模块/FFI）。
5. **工作量粗估**：方案 A 约 **9–16 人月**（顺序；±50%），
   纯 Rust 重写约 18–30 人月；板载虚拟化当前被厂商 Mesa 的 EGL
   core-profile 配置阻塞，不是短期捷径。
6. **第一个可证伪里程碑**：P1 的"释放 META → 观察到固件 started 标志"
   板级 marker（4–6 周）。若 P1 不过，停止后续投入。

## 2. 参考栈证据（RockOS）

| 项 | 值 |
|---|---|
| 内核 | `6.6.87-win2030`（`/boot/vmlinuz-6.6.87-win2030-origin`） |
| GPU | AXM-8-256，BVNC `30.3.408.101`，`/soc/gpu@51400000`，`compatible=img,gpu` |
| KMD | `pvrsrvkm.ko`，vermagic `6.6.87-win2030`，自动加载 |
| 固件 | `rgx.fw.30.3.408.101`、`rgx.sh.30.3.408.101` |
| 节点 | `card1`（pvrsrvkm）、`renderD128`；显示 `card0` = `es_drm` |
| Xorg | `glamor X acceleration enabled on PowerVR A-Series AXM-8-256` |
| 用户态 | `eswin-eic7x-gpu 24.2+0rockos3`；Mesa `22.3.5+1rockos1+0pvr2`（`pvr_dri.so`） |

关键对照：RockOS 默认引导的自定义 `6.6.87` 内核**不匹配**上述模块
vermagic，GPU 不加载；匹配的包内核一直保留在 `/boot`。

## 3. 厂商源码拆解

稀疏克隆 `eswincomputing/linux-stable:linux-6.6.18-EIC7X` 的
`drivers/gpu/drm/img`：888 个文件、435,840 行 C/H、19 MB。

| 层 | 文件 | 行数 | 处理方式 |
|---|---:|---:|---|
| `services/server/common`（DDK 服务核心） | 50 | 70,580 | 复用（MIT） |
| `services/server/env/linux`（Linux OS/DRM 适配） | 72 | 34,391 | **替换**（Rust/Asterinas） |
| `services/shared` | 35 | 17,720 | 复用（MIT） |
| `services/system`（SoC/OS 抽象） | 60 | 19,989 | 部分复用；Linux 部分替换 |
| `services/display` | 24 | 15,114 | 显示集成，按需 |
| `include` | 95 | 37,679 | 复用（UAPI/ABI） |
| `hwdefs` | 149 | 41,811 | 复用（寄存器定义） |
| `generated` | 170 | 76,922 | 复用（bridge/ABI 生成层） |

`services/server/env/linux` 的替换面包括：
`pvr_drm.c`（DRM 驱动与 ioctl）、`pvr_bridge_k.c`（bridge 分发）、
`physmem_osmem_linux.c` / `physmem_dmabuf.c` / `pmr_os.c`（内存/PRIME）、
`fwload.c`（固件加载）、`pvr_buffer_sync.c` / `pvr_counting_timeline.c`
（sync/fence）、`osfunc*.c`（OS 抽象）、`pvr_dvfs*.c`（电源/频率）、
debugfs 等。

UAPI 形态：驱动在 DRM render node 上注册
`PVR_SRVKM_CMD`（bridge 分发）、`PVR_SRVKM_INIT`、`PVR_SYNC_*`、
`PVR_SW_SYNC_*` 等 ioctl，全部带 `DRM_RENDER_ALLOW`
（`services/server/env/linux/pvr_drm.c:435-454`）。厂商用户态
（`libsrv_um`、`libusc`、`libGLESv2_PVR_MESA`、`libpvr_dri_support`）
按该 ABI 打开 `renderD128`。

## 4. Asterinas 现状与差距

| 能力 | 现状 | 差距 |
|---|---|---|
| DRM 设备 | 单设备 `card0`/`renderD128`（virtio-gpu/simpledrm） | 需要多 DRM 设备、minor 分配、sysfs 拓扑与权限拆分 |
| GPU 上电/时钟 | `powervr_probe/power.rs` 已实现 CRG/ID/DMA/META/MMU 表 | 未释放 META、未观察固件 started |
| 固件加载 | 无内核固件加载设施；镜像需自带 | 需要 `/lib/firmware` 读取或 initramfs 内嵌 |
| MMU/FWIF | `mmu.rs`/`fwif.rs` 已有 Volcanic MMUv4 与 FWIF 地址契约 | 命令队列、页表安装、堆管理 |
| C/FFI | 内核组件无 `cc` 构建依赖（仅宏 build.rs） | 需要引入受控 C 构建/FFI 边界 |
| PRIME/dma-buf | virtio-gpu 路径已有 GEM/PRIME | PVR 设备与显示设备间的 PRIME 共享 |
| 同步 | 现有 dma-fence 基础设施 | 厂商 sync ioctl 与 dma-fence 映射 |
| 用户态 | 板级 rootfs 为 Debian Mesa 25.0.7，无 `pvr_dri.so` | 需引入 RockOS 的 `eswin-eic7x-gpu` + Mesa PVR 包 |
| 显示 | fbdev/软件渲染（已通过的 E3 板测） | Xorg modesetting + glamor on PVR render node |

存量探测的可复用点（Rust，`kernel/src/device/dri/powervr_probe/`）：
DT 校验、CRG 门控、ID 读取、DMA 别名/40-bit mask、META 复位与总线准备、
MMUv4 表准备、firmware catalogue。它已经覆盖了方案 A 中"OS 底层"
的大部分工作，是 P1/P2 的起点。

## 5. 架构选项

### 方案 A（推荐）：厂商 MIT C 核心 + Rust OS/DRM 层

- **复用**：`server/common`、`shared`、`include`、`hwdefs`、`generated`
  的 MIT C 代码（约 24 万行），保持与厂商 UM 的 ABI 一致。
- **替换**：`server/env/linux`（约 3.4 万行）与 `system` 的 Linux 部分，
  用 Rust 实现 Asterinas OS 抽象：内存/PMR、mmap、ioctl 传输、IRQ、
  workqueue、电源/时钟（复用探测）、固件加载。
- **DRM**：在 `kernel/src/device/dri.rs` 之外新增 PowerVR DRM 设备，
  注册厂商 ioctl；显示仍走现有 simpledrm/KMS 卡。
- **安全边界**：把 C 核心隔离在一个受控组件（unsafe/FFI 边界、
  窄接口、静态生命周期），在 framekernel 政策中显式登记；安全 Rust
  侧不直接依赖 C 内部结构。
- **许可**：MIT 分支与 MPL-2.0 兼容；保留版权与许可声明。
- **风险**：C-in-frame 政策、ABI 版本锁定、构建系统改造。

### 方案 B：纯 Rust 重写

- 以厂商源码与现有探测为参考，逐层用 Rust 实现；安全但工作量最大
  （18–30 人月），且 GUI 用户态 ABI 需要与厂商二进制逐字节对齐。
- 仅在 framekernel 明确拒绝 C 模块时选择。

### 方案 C：板载虚拟化（RockOS 宿主 + KVM + virgl + Asterinas guest）

- 复用已完成的 QEMU E3 栈；不需要把 KMD 移植进 Asterinas。
- 当前阻塞：QEMU `egl-headless` 在 `renderD128` 上仍报
  `eglChooseConfig failed (core mode)`（厂商 Mesa 只有 GLES 配置）。
  需要先用 GLES 能力重建 virglrenderer/QEMU，或提供满足 core-profile
  的 EGL；此外 origin 内核没有 `/dev/kvm`，自定义内核有 KVM 但 GPU 模块
  vermagic 不匹配——两条路都要先解决内核/模块版本配对。
- 定位：如果目标接受"虚拟机内的加速 Firefox"，可作为并行 spike；
  不能替代原生移植结论。

### 方案 D：停在软件渲染

- 现状（E3 板测已通过）保持不变；本计划不执行。

## 6. 分阶段计划（方案 A）

每个阶段都以**板级运行时 marker** 为验收，不以源码/单元测试代替。

### P0 — 多 DRM 设备与 render node 基础设施

- 交付：DRM 核心支持第二个设备（card1/renderD129 或按 minor 分配），
  独立 driver name、sysfs 拓扑、render 权限拆分；GEM/PRIME 对象可跨
  显示设备与 GPU 设备导出/导入。
- 验收：`/dev/dri/card1` + `renderD129` 枚举；`drmGetVersion`、
  `drmGetDevices2` 可见；PRIME export/import 在两张卡间成功；现有
  virtio-gpu/simpledrm 回归不破。
- 估时：3–5 周。
- 风险：现有 `dri.rs` 单设备假设较深（`dri.rs:5,213-214`），
  sysfs/权限模型需要重构。

### P1 — 固件加载、上电与 META 释放（第一个 go/no-go）

- 交付：Asterinas 能从 rootfs/initramfs 读取 `rgx.fw.30.3.408.101`
  与 `rgx.sh.30.3.408.101`；复用探测的 CRG/ID/DMA/MMU 准备；释放
  META，轮询固件 started 标志并读取固件版本；失败可逆（保持复位、
  恢复 CRG）。
- 验收：串口出现新的
  `ASTERINAS_POWERVR_FW_STARTED status=ok bvnc=30.3.408.101 ...`
  （或等价 marker），且失败路径有明确 reason；不触碰显示。
- 估时：4–6 周（探测已覆盖大半）。
- 停止条件：若 META 释放后无法在有限时间内观察到 started/固件响应，
  停止后续阶段，回到方案 C/D。
- 风险：固件 ABI/签名、META 复位时序、固件镜像许可与分发。

### P2 — MMU/堆/FWIF、命令队列与一个 no-op 作业

- 交付：固件页目录安装、内核/固件堆、命令队列（KCCB/CCCB）、
  中断处理、一个由内核发起的 no-op 或清屏作业 + 完成 fence。
- 验收：板级 marker 证明作业提交与完成（固件日志/中断计数），
  不依赖用户态。
- 估时：8–12 周。
- 风险：MMUv4 页表/缓存一致性、中断路由、固件命令 ABI。

### P3 — Bridge UAPI 与 DDK 核心集成（架构决策门）

- 前置：framekernel 政策批准受控 C/FFI 组件（决策 D1）。
- 交付：DRM render node 上的 `PVR_SRVKM_CMD`/`PVR_SRVKM_INIT`/sync
  ioctl；bridge 分发接入 DDK 核心；PMR/内存对象、参数/设备信息查询。
- 验收：厂商 `pvrsrvctl --start` 能识别设备；最小 bridge 调用完成；
  无内核崩溃/泄漏。
- 估时：12–20 周。
- 风险：bridge ABI 与 DDK 24.2 UM 必须逐字节一致；C 核心的
  Linux 依赖剥离面大。

### P4 — PRIME/dma-buf 与同步

- 交付：GEM/PMR 的 dma-buf 导出/导入；厂商 sync ioctl 与 dma-fence
  映射；跨设备（显示卡 ↔ GPU 卡）共享；超时/错误路径。
- 验收：两张卡间的 PRIME 往返 + fence 等待成功；故障注入可回滚。
- 估时：4–6 周。

### P5 — 厂商用户态/Mesa PVR 最小 bring-up

- 交付：板级 rootfs 增加 `eswin-eic7x-gpu`（UM 库、固件、Xorg 配置）
  与 RockOS Mesa `22.3.5+0pvr2`（`pvr_dri.so`），保留 fbdev 回退；
  `eglconfigs`/`rgx_triangle_test` 或等价 GLES 测试通过。
- 验收：EGL 初始化 + GLES 三角形渲染成功，renderer 字符串为
  PowerVR（非 llvmpipe/swrast）；fbdev 桌面仍可启动。
- 估时：4–8 周。
- 风险：RockOS 包与 Debian 13 rootfs 的依赖/ABI；GLES-only 与
  Firefox 所需 GL 能力的差距。

### P6 — Xorg glamor + Firefox WebRender

- 交付：Xorg modesetting 使用 Asterinas 显示卡（`kmsdev=/dev/dri/card0`），
  glamor 走 PVR render node；Firefox ESR 以 EGL/GLES 启用 WebRender，
  跑固定本地场景。
- 验收：Xorg 日志出现 PowerVR glamor；Firefox `about:support` 显示
  WebRender available、renderer 非软件；场景截图/帧时间可重复。
- 估时：6–10 周。
- 风险：GLX/EGL 客户端路径（RockOS 参考栈上 `glxinfo`/`xeglinfo`
  尚未完全证明）、WebRender 对 GLES 版本/扩展的要求、性能。

## 7. 工作量、依赖与风险

| 阶段 | 估时（人月） | 关键依赖 |
|---|---:|---|
| P0 多 DRM | 0.7–1.2 | 现有 DRM/GEM/PRIME 重构 |
| P1 固件启动 | 1.0–1.5 | 固件镜像/许可、探测代码 |
| P2 MMU/FWIF/作业 | 2.0–3.0 | 固件 ABI、中断/DMA |
| P3 Bridge/DDK | 3.0–5.0 | D1 政策、C 构建 |
| P4 PRIME/sync | 1.0–1.5 | P0/P2 |
| P5 UM/GLES | 1.0–2.0 | rootfs 包、ABI |
| P6 Xorg/Firefox | 1.5–2.5 | P5、QEMU 侧 Firefox 工作 |
| **合计** | **9–16** | ±50%，不含维护 |

主要风险（按影响排序）：

1. **framekernel 政策**：C DDK 与安全 Rust 内核的边界是项目级决策；
   若不允许，方案 A 不成立，转方案 B（成本翻倍）或停止。
2. **ABI 锁定**：bridge 与 UM 二进制版本必须一致；DDK 24.2 的
   `generated` 层是唯一权威。
3. **多 DRM/PRIME**：Asterinas 目前单 DRM 设备；显示卡与 GPU 卡的
   PRIME 共享是 P0/P4 的核心。
4. **用户态 GL 能力**：厂商 Mesa 是 GLES 栈；Firefox WebRender 需要
   EGL/GLES3 + 合适的配置。RockOS 上 glamor 成功、GLX 客户端未证明，
   是 P5/P6 的前置未知。
5. **维护成本**：43.6 万行厂商 C 代码的长期跟踪与安全审计。
6. **固件/许可**：`rgx.fw/sh` 的再分发条款需确认；MIT 只覆盖源码。

## 8. 决策门

- **D1（P3 前）**：是否允许受控 C/FFI 组件进入 framekernel；
  安全边界与构建方式定稿。
- **D2（P1 后）**：板级固件 started marker 是否出现；否则停止。
- **D3（P5 后）**：厂商 UM + GLES 三角形是否在 Asterinas 上跑通；
  否则停止或转方案 C。
- **D4（P6 前）**：Firefox WebRender 是否接受 PVR 的 EGL/GLES
  能力；必要时先在 RockOS 上做 Firefox 加速对照。

## 9. 建议的下一步（bounded spike）

在不动大架构的前提下，先做 **P1 的板级固件启动 spike**：

1. 在现有 `powervr_probe` 上增加"释放 META + 观察 started"路径，
   保持 opt-in（`asterinas.powervr=1`）与可逆失败；
2. 把 `rgx.fw/sh` 放入板级 initramfs/rootfs（先验证许可）；
3. 用现有 `megrez_desktop_boot` 的 manual boot 方式，在板子上采集
   `ASTERINAS_POWERVR_FW_STARTED`；
4. 若 marker 出现，进入 P0/P2 的详细实现计划（届时用 writing-plans
   产出逐任务、带测试的实现计划）；若不出现，记录失败边界并停止。

这一步不需要多 DRM、不需要用户态、不需要 C 核心，却能把"原生移植
是否值得投入"从推测变成实测。
