# PowerVR GLES/EGL 后端路线评估（Megrez P3/B5，2026-10-22）

## 结论

推荐 **路线 a：使用厂商 UMD 二进制，在 Asterinas 上补齐它要求的 DRM/PVR
ABI**。但必须按“厂商自测程序 → Xorg glamor → Firefox WebRender”分段验收，
而不是直接冲 Firefox。厂商栈已经包含完整的 GLES 3.x UMD、EGL/DRI 支撑、
固件和 Xorg 配置，且其 changelog 明确修过 “firefox gpu shader”，是唯一
已有现实用户态实现的路径；代价集中在**内核侧 PVR bridge/渲染上下文**，
而不是重新实现 GLES。

第一条可行路径的最小工作量（粗估）：先让**一个厂商 EGL/GLES 测试程序**
在 `renderD128` 上跑通，需要 8 条 PVR DRM ioctl、约 20–30 条 bridge 命令、
按 PMR handle 的 mmap、事件等待接入 `poll`、dma-buf 导入/导出与 cache
维护；按当前“单人 + agent、每轮 bounded”的节奏约 **3–5 周**。Xorg glamor
与 Firefox 的完整链路在其后，另有 2–4 周的不确定性（gbm/EGL 平台扩展、
DMA_BUF_IOCTL_SYNC、page-flip/事件、cache 一致性）。

## 0. 本轮取证范围

- 板子当晚在 U-Boot/autoboot 与串口之间反复不稳定，**没有从板子拉取
  二进制**；`libsrv_um`/`libusc`/`pvr_dri` 的板级版本核对留到物理复位后。
- 作为替代，取回 RockOS 官方 GPU 用户态包源码/二进制：
  `https://github.com/rockos-riscv/eswin-eic7x-gpu`，commit
  `3232bf704e146627bc7155a1e68f2fd4c57f4fc0`，包版本
  `24.2+0rockos5`，本地路径
  `/mnt/shared/xaj/vendor/rockos-eic7x-gpu-20261022/`。
- 该包内容与板子上的 DDK 24.2 对应：`libsrv_um.so.24.2.6643903`、
  `libusc.so`、`libGLESv2_PVR_MESA.so`、`libGLESv1_CM_PVR_MESA.so`、
  `libpvr_dri_support.so`、`libVK_IMG.so`、`pvrsrvctl`、
  `rgx.fw.30.3.408.101` / `rgx.sh.30.3.408.101`。
- 观察方法：`readelf -d/--dyn-syms`、`strings -a`、少量
  `riscv64-linux-gnu-objdump -d`（只到确认 ioctl 编号为止）。

## 1. 客户端需要什么

### 1.1 Firefox WebRender

- 在 Linux/X11 上，WebRender 的 GL 后端需要一个可用的 EGL 或 GLX
  上下文；Firefox 的 EGL 路径要求平台扩展
  `EGL_EXT_platform_x11` / `EGL_KHR_platform_gbm` /
  `EGL_MESA_platform_surfaceless` 之一，以及 GLES2/3 入口。
- 合成/共享纹理需要 `EGL_EXT_image_dma_buf_import`（dmabuf 导入），
  fence 等待需要 `EGL_KHR_fence_sync` 或
  `EGL_ANDROID_native_fence_sync`；跨进程共享依赖 PRIME/dmabuf。
- 若 EGL/GLES 不可用，Firefox 会退回软件 WebRender/SWGL；当前板上正是
  这条路径，所以“有 EGL 库”不等于“能加速”，还要有可用的渲染上下文与
  完成通知。

### 1.2 Xorg glamor

- 现代 Xorg modesetting 驱动优先用 glamor + EGL（`EGL_PLATFORM_GBM_KHR`）
  或 GLX；它需要：
  - gbm 设备：`gbm_create_device(drm_fd)`、`gbm_bo_create/map/get_fd`；
  - EGL：`eglGetPlatformDisplayEXT(EGL_PLATFORM_GBM_KHR, gbm_dev)`、
    `eglCreateImageKHR`（dmabuf）、GLES2 shader/pipeline；
  - DRM：`CREATE_DUMB/MAP_DUMB/ADDFB`、PRIME 导入导出、
    `DMA_BUF_IOCTL_SYNC`；
  - 同步：`glFenceSync/glClientWaitSync`（或 EGL fence），以及
    page-flip 完成事件。
- 没有硬件 EGL 时，glamor 会退回 llvmpipe/swrast；当前 Xorg 能在板上
  工作，说明软件路径已通，但加速路径未验证。

### 1.3 共同的 DRM/EGL/GLES 面

| 面 | WebRender | glamor | Asterinas 现状（B3/B4） |
|---|---|---|---|
| EGL display/context | 需要 | 需要 | 无（用户态） |
| GLES2/3 入口 | 需要 | 需要 | 无（用户态） |
| gbm | 可选/常用 | 需要 | 无（用户态） |
| GEM 分配/mmap | 需要 | 需要 | 部分：临时 PVR ioctl 0x50 + pool 偏移 mmap |
| PRIME 导入/导出 | 需要 | 需要 | 已有通用 `PRIME_*`，但未接 PVR PMR |
| dmabuf CPU sync | 需要 | 需要 | 缺 `DMA_BUF_IOCTL_SYNC` |
| fence/同步 | 需要 | 需要 | 部分：syncobj + 内核 fence；无 fd 导出/poll |
| 完成事件到 `poll(drm_fd)` | 需要 | 需要 | 缺：DRM fd 的 `poll` 永远为空 |
| KMS/page-flip 事件 | 需要 | 需要 | 部分：同步 PAGE_FLIP，无事件队列 |

## 2. 厂商二进制对内核/用户态的要求

从 DDK 24.2 包直接观察到的硬性要求：

- **设备节点**：`libsrv_um` 字符串含 `/dev/dri/card%d` 与
  `/dev/dri/renderD%d`，并用 `drmGetDevices2`（libdrm）枚举；打开后
  直接 `ioctl`。
- **PVR ioctl**：确认 `DRM_IOCTL_PVR_SRVKM_INIT = 0x40046445`
  （`lui a1,0x40046; addi a1,0x445` 后调用 `ioctl`）；所有 services
  调用经 `PVRSRV_BRIDGE` 走 `DRM_IOCTL_PVR_SRVKM_CMD = 0xc0206440`
  （uapi 32 B `struct drm_pvr_srvkm_cmd`）。
- **bridge 组**（ID 来自 `services/include/pvr_bridge.h` /
  `rgx_bridge.h`）：
  - `SRVCORE=1`（CONNECT 0、EVENTOBJECTWAIT 5/13、GETDEVICESTATUS 11、
    ACQUIREINFOPAGE 15）；
  - `SYNC=2`（ALLOCSYNCPRIMITIVEBLOCK 0、SYNCPRIMSET 2）；
  - `MM=6`（PHYSMEMNEWRAMBACKEDPMR 8、DEVMEMINTCTXCREATE 9、
    DEVMEMINTHEAPCREATE 11、DEVMEMINTMAPPMR 13、GETDEFAULTPHYSICALHEAP 27）；
  - `DMABUF=11`（IMPORT/EXPORT）；
  - `CACHE=13`（CACHEOPQUEUE 0、CACHEOPEXEC 1）；
  - `RGXCMP=129`（RGXKICKCDM 9、RGXFLUSHCOMPUTEDATA 2）；
  - `RGXTA3D=130`（RGXCREATERENDERCONTEXT 14、RGXCREATEFREELIST 13、
    RGXKICKTA3D2 10、RGXCREATEZSBUFFER 1）；
  - `RGXKICKSYNC=136`（CREATE 0、KICKSYNC2 2）；
  - `PVRTL=15`（可选，首像素不需要）。
- **mmap**：`vm_pgoff` 是 secure PMR handle，不是字节偏移；内核必须
  `PVRSRVLookupHandle(PHYSMEM_PMR)` 后映射。Asterinas 现在的 pool-offset
  mmap 不满足这个 ABI。
- **dma-buf**：`libpvr_dri_support` 导入
  `drmPrimeHandleToFD`/`drmPrimeFDToHandle`/`drmIoctl`/`drmGetVersion`/
  `drmGetCap`；dma-buf ops 需要 `begin/end_cpu_access`，即
  `DMA_BUF_IOCTL_SYNC`。
- **同步**：`libsrv_um` 导入 `poll`，等待事件对象；`libpvr_dri_support`
  含 `common/env/linux/libsync_libdrm.h`、`sw_sync` 和
  `CreateFenceFd`/`FenceWait`；`libsrv_um` 的调试串包含
  `sync_wait failed on fence %d`。因此需要可 `poll` 的 DRM fd 事件、
  fence fd（或等效）和 `sw_sync` 时间线。
- **用户态依赖**：`libsrv_um`/`libpvr_dri_support` 只依赖 `libdrm.so.2`
  和 glibc（无 gbm 依赖）；`libGLESv2_PVR_MESA` 导出 358 个
  `gl*` 入口、**不导出 `egl*`**，EGL 由系统的 libEGL/glvnd 或
  Mesa EGL 分发，再加载 PVR 的 DRI/GLES 实现（`libpvr_dri_support`
  导出 `KEGL*`、`PVRDRIRegister*`）。
- **发行配置**：Xorg 用 `modesetting` + `kmsdev=/dev/dri/card0`；
  `pvrsrvkm.service` insmod `pvrsrvkm.ko`；Vulkan ICD 指向
  `libVK_IMG.so`；包 changelog 有 “fix firefox gpu shader”。

## 3. 差距表

| 需求 | Asterinas B3/B4 现状 | 厂商库要求 | 差距/代价 |
|---|---|---|---|
| `renderD128` 节点 | 已有 | 打开 render node | 节点已有；需确认 sysfs/drmGetDevices2 发现 |
| PVR `SRVKM_INIT`/`SRVKM_CMD` | 缺 | 8 条 PVR ioctl 的唯一漏斗 | 中：编码/分发/大小校验/白名单 |
| PVR bridge 分发与连接 | 缺 | 135 条命令（图形子集）；至少 20–30 条首像素 | 大：逐命令实现 + 用户指针拷贝 + 状态机 |
| PMR 分配/映射 | 部分：B3 池对象 + 外部 MMU 映射 | `PHYSMEMNEWRAMBACKEDPMR` + secure PMR handle mmap | 大：PMR 生命周期、handle 表、mmap 按 pgoff |
| GPU 渲染上下文/kick | 缺（B2 仅 SLC flush） | `CREATERENDERCONTEXT`/`CREATEFREELIST`/`RGXKICKTA3D2` | 大：firmware CCB、freelist、ZS、MMU cache、IRQ 完成 |
| 事件对象/poll | 缺（DRM fd poll 恒空） | `EVENTOBJECTWAIT` + `poll(drm_fd)` | 中：event object + Pollee 通知 |
| fence/时间线 | 部分：syncobj + 内核 fence（B4） | sw_sync、fence fd、`sync_wait` | 中：fd 导出、时间线推进、`poll` 唤醒 |
| dma-buf 导入/导出 | 部分：通用 PRIME 已有 | `DMABUF` bridge + `DMA_BUF_IOCTL_SYNC` | 中：接 PVR PMR、cache 维护 |
| cache 操作 | 缺 | `CACHEOPQUEUE/EXEC` | 中：DMA 缓冲 clean/invalidate |
| GBM/EGL/GLES 用户态 | 缺 | 厂商 UMD + 系统 libEGL/Mesa | 用户态可复用厂商包；依赖内核 ABI |
| KMS/page-flip 事件 | 部分：同步 flip | glamor vsync | 小–中：事件队列 + poll |
| 固件/内核服务 | P2 已启动固件、KCCB/IRQ 可用 | pvrsrvkm 的 PFIFO/MMU/电源语义 | 中：把 probe 代码升级为服务 |

## 4. 三条候选路线

### a. 厂商 UMD 二进制 + 补 Asterinas ABI（推荐）

- **优点**：唯一已有完整 GLES3/EGL/Vulkan 实现且针对本 SoC/固件编译；
  changelog 修过 Firefox shader；用户态零重写；性能上限最高。
- **缺点/风险**：需要实现大量 PVR 专有内核语义（bridge、PMR、event、
  fence、cache、firmware render context）；二进制锁定 DDK 24.2，
  升级需同步；部分 Linux 行为（dma-buf、sync_file、sysfs、proc）要在
  Asterinas 补齐；闭源二进制调试困难。
- **最小第一路径**：`SRVKM_INIT` + `SRVKM_CMD` 分发 + CONNECT + 内存
  PMR + `eglconfigs`/`xgles2test1` 所需的最小 bridge；验收是**厂商
  自测程序出图**，不是 Firefox。粗估 3–5 周；之后 glamor 2–4 周。

### b. 上游 Mesa 自建 pvr 后端

- **事实**：上游 Mesa 的 `src/imagination` 是 **Vulkan** 驱动，依赖
  Linux `drm/imagination` UAPI；公开支持 AXE-1-16M（33.15.11.3）与
  BXS-4-64（36.53.104.796），**不支持 AXM-8-256 (30.3.408.101)**；
  上游没有 PowerVR GLES/gallium 驱动。
- **需要**：先实现 `drm/imagination` UAPI + 上游 Vulkan 驱动适配 BVNC，
  再考虑 zink（Vulkan→GLES）或新写 gallium 驱动。
- **评价**：工作量和不确定性都大于 a，且 AXM-8-256 支持要自己加；
  适合作为长期开源替代，不是第一条可行路径。

### c. 自写最小 GLES/EGL 桩仅够 WebRender/glamor

- **做法**：实现 EGL 1.5 + GLES2 子集，后端用软件光栅（llvmpipe/
  pixman）或转发到厂商 UMD。
- **优点**：不依赖 PVR 内核语义，能快速让客户端“看到 EGL”。
- **缺点**：没有硬件加速；GLES 入口数百个、状态机复杂，工作量并不小；
  最终仍要回到 a 才能用 GPU。可作为临时兼容层，但不是目标路径。

## 5. 推荐

**走路线 a，分段推进：**

1. **A1（1–2 天）**：PVR ioctl 骨架（8 条命令号、`SRVKM_INIT`、
   `SRVKM_CMD` 的 in/out 校验），render node 白名单；板级用
   `pvrsrvctl` 验证 connect。
2. **A2（1 周）**：SRVCORE/MM 最小集（CONNECT、GETDEVICESTATUS、
   PMR 分配/映射、secure PMR handle mmap），验收 `eglconfigs` 能枚举
   EGL config。
3. **A3（1–2 周）**：RGXTA3D/RGXCMP/KICKSYNC + firmware render context、
   freelist、MMU cache、IRQ 完成；验收 `rgx_triangle_test`/`xgles2test1`
   出图。
4. **A4（1 周）**：event object + DRM fd `poll`、sw_sync/fence fd、
   dma-buf import/export + `DMA_BUF_IOCTL_SYNC`；验收 Xorg glamor 用
   EGL 渲染。
5. **A5**：Firefox WebRender EGL 路径；保留软件回退作为兜底。

**关键风险与止损点**：

- 若 A3 的 firmware render context/kick 在板级无法稳定完成，停止在
  A2，把 PVR 定位为“可分配/可映射但无渲染”，不宣称 GLES；
- 若厂商二进制要求 Asterinas 无法提供的 Linux 语义（如
  `sync_file`、特定 ioctl 返回布局），记录为 B5a 并评估用
  `libhybris` 式兼容层的代价；
- 板级每一步只认厂商测试程序的输出，不以 Firefox 启动作为中间门禁。

## 6. 边界与下一步

- 本轮是**路线评估**，未实现 PVR bridge，也未从板子取二进制；板级
  版本核对与 `pvrsrvctl` 冒烟等物理复位后补。
- 未实现 vendor `PVR_SRVKM` 专有 sync bridge；B4 的通用 syncobj/fence
  可继续作为内核完成通知的基础。
- 下一步建议直接开 **A1**（ioctl 骨架 + connect 冒烟），这是最小、
  可回退、且能立即验证厂商二进制与本内核 ABI 是否对得上的门禁。

## 参考

- RockOS GPU 用户态包：<https://github.com/rockos-riscv/eswin-eic7x-gpu>
  （commit `3232bf704e146627bc7155a1e68f2fd4c57f4fc0`，包 `24.2+0rockos5`）
- 厂商 UAPI/bridge 源码：`/mnt/shared/xaj/vendor/eswin-src-20261022/`
  （`include/drm/pvr_drm.h`、`services/include/pvr_bridge.h`、
  `rgx_bridge.h`、`generated/volcanic/*`）
- 上游 Mesa PowerVR：`/mnt/shared/xaj/vendor/mesa-upstream-20261022/`
  （`src/imagination`，仅 Vulkan）
- P3 UAPI 差距清单：`docs/porting/evidence/2026-10-22-p3-uapi-gap-inventory.md`
- 本机 GPU 软件栈调查参考：ESWIN GPU User Guide v0.6、
  Gentoo Milk-V Megrez wiki
