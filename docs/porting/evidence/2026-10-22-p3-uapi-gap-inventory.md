# P3 前置：厂商 UM DDK 24.2 UAPI 与 Asterinas 差距清单（2026-10-22）

> 中立预研，不实现、不选边。P3 的 A/B 路线仍待人工拍板。本文件只回答三个问题：
> 厂商 UM DDK 暴露了哪些接口面、Asterinas 现在有什么、差什么。

## 0. 范围与方法

- 厂商源码：`eswincomputing/linux-stable`，分支 `linux-6.6.18-EIC7X`，
  HEAD `fc6038c00e006226e3bd504d2679c534eabf5503`，稀疏路径
  `drivers/gpu/drm/img/img-volcanic`、`arch/riscv/boot/dts/eswin`、
  `include/uapi/drm`（共 1020 个文件）。UM 侧二进制为 DDK 24.2 的
  `libsrv_um`/`libusc`；本树是与其匹配的 KMD/UAPI 源码。
- 配置：`config_kernel.mk` 的 `eswin_linux` release 构建，BVNC
  `30.V.408.101`，`RGX_NUM_DRIVERS_SUPPORTED=1`，`SUPPORT_NATIVE_FENCE_SYNC=1`，
  `SUPPORT_DMA_FENCE=1`，`SUPPORT_BUFFER_SYNC=1`。
- 计数方法：
  - DRM ioctl 从 `include/drm/pvr_drm.h` 的 `DRM_IOCTL_PVR_*` 宏直接数；
  - bridge 命令从 `generated/volcanic/*/common_*_bridge.h` 的
    `PVRSRV_BRIDGE_<GROUP>_CMD_LAST` 反推（`LAST = FIRST + N`，条目数 `N+1`）；
  - 生成头是“全功能”集合，实际编译进去的组由 `pvr_bridge.h` 的
    `#if` 与 `config_kernel.*` 决定；下文同时给出“生成总数”和“本配置可能启用
    的图形子集”，后者才是 UM DDK 真正可能调用的范围。

## 1. 厂商 UM DDK 24.2 的 UAPI 清单

### 1.1 DRM 节点与 DRM ioctl（8 条）

- 驱动特性：`DRIVER_RENDER | DRIVER_GEM | DRIVER_PRIME`
  （`services/server/env/linux/pvr_drm.c:537`）。
- 节点：`card0`（minor 0）+ `renderD128`（minor 128）；PVR 的 8 条 ioctl
  全部带 `DRM_RENDER_ALLOW`，其中 `DRM_IOCTL_PVR_SRVKM_CMD` 是所有
  services 调用的唯一漏斗。
- 文件：`include/drm/pvr_drm.h`（UAPI），
  `services/server/env/linux/pvr_drm.c:435-455`（ioctl 表）。

| # | ioctl（DRM_COMMAND_BASE=0x40） | nr | 方向/大小 | 用途 |
|---|---|---|---|---|
| 1 | `DRM_IOCTL_PVR_SRVKM_CMD` | 0x40 | IOWR, 32 B | 通用 bridge 调用：`bridge_id`/`bridge_func_id`/in/out 指针与长度 |
| 2 | `DRM_IOCTL_PVR_SYNC_RENAME_CMD` | 0x41 | IOW, 32 B | 重命名 sync 对象 |
| 3 | `DRM_IOCTL_PVR_SYNC_FORCE_SW_ONLY_CMD` | 0x42 | IO | 强制软件同步 |
| 4 | `DRM_IOCTL_PVR_SW_SYNC_CREATE_FENCE_CMD` | 0x43 | IOWR, 48 B | 创建 sw_sync 时间线 fence fd |
| 5 | `DRM_IOCTL_PVR_SW_SYNC_INC_CMD` | 0x44 | IOR, 8 B | 推进时间线 |
| 6 | `DRM_IOCTL_PVR_SRVKM_INIT` | 0x45 | IOW, 4 B | render 设备按需初始化 |
| 7 | `DRM_IOCTL_PVR_EXP_FENCE_SYNC_FORCE_CMD` | 0x46 | IOWR, 48 B | 强制 export-fence 模式 |
| 8 | `DRM_IOCTL_PVR_SYNC_CREATE_EXPORT_FENCE_CMD` | 0x47 | IOWR, 48 B | 创建可导出的 dma_fence |

### 1.2 Bridge 命令面（真正的 UAPI 主体）

`struct drm_pvr_srvkm_cmd` 把 `bridge_id`（命令组）与 `bridge_func_id`
（组内函数）分开；in/out 是用户态指针，由内核 `copy_from_user`/
`copy_to_user` 语义搬运（见 `pvr_bridge_k.c:PVRSRV_BridgeDispatchKM`）。
UM DDK 的绝大多数操作都走这一条 ioctl。

- **生成集合**：31 个组、**254 条命令**（`generated/volcanic/*`）。
- **本配置可能启用的图形子集**：**119 条**；
  加辅助组（DI/SYNCTRACKING/DEVICEMEMHISTORY/CMM/MMEXTMEM）为 **135 条**。
  注意：这是“API 面”的上界，具体 UM 二进制会按功能裁剪调用。

| 组 | 条目 | 关键文件 | 典型命令 |
|---|---:|---|---|
| SRVCORE | 17 | `generated/volcanic/srvcore_bridge/common_srvcore_bridge.h` | `CONNECT`、`DISCONNECT`、`EVENTOBJECTWAIT`、`ACQUIREINFOPAGE`、`GETDEVICESTATUS` |
| MM | 36 | `generated/volcanic/mm_bridge/common_mm_bridge.h` | `PHYSMEMNEWRAMBACKEDPMR`、`PMREXPORT/IMPORT`、`DEVMEMINTHEAPCREATE`、`DEVMEMINTMAPPMR`、`DEVMEMXINT*` |
| SYNC | 10 | `generated/volcanic/sync_bridge/common_sync_bridge.h` | `ALLOCSYNCPRIMITIVEBLOCK`、`SYNCPRIMSET`、`SYNCALLOGEVENT` |
| DMABUF | 4 | `generated/volcanic/dmabuf_bridge/common_dmabuf_bridge.h` | `PHYSMEMIMPORTDMABUF`、`PHYSMEMEXPORTDMABUF`、`PHYSMEMEXPORTGEMHANDLE`、`PHYSMEMIMPORTSPARSEDMABUF` |
| CACHE | 3 | `generated/volcanic/cache_bridge/common_cache_bridge.h` | `CACHEOPQUEUE`、`CACHEOPEXEC`、`CACHEOPLOG` |
| PVRTL | 8 | `generated/volcanic/pvrtl_bridge/common_pvrtl_bridge.h` | `TLOPENSTREAM`、`TLRESERVESTREAM`、`TLCOMMITSTREAM`、`TLWRITEDATA` |
| RGXTA3D | 16 | `generated/volcanic/rgxta3d_bridge/common_rgxta3d_bridge.h` | `CREATERENDERCONTEXT`、`RGXKICKTA3D2`、`CREATEFREELIST`、`CREATEZSBUFFER` |
| RGXCMP | 12 | `generated/volcanic/rgxcmp_bridge/common_rgxcmp_bridge.h` | `CREATECOMPUTECONTEXT`、`RGXKICKCDM`、`RGXCDMGETSHAREDMEMORY` |
| RGXTQ2 | 9 | `generated/volcanic/rgxtq2_bridge/common_rgxtq2_bridge.h` | `RGXTDMCREATETRANSFERCONTEXT`、`RGXTDMSUBMITTRANSFER2/3` |
| RGXKICKSYNC | 4 | `generated/volcanic/rgxkicksync_bridge/common_rgxkicksync_bridge.h` | `RGXCREATEKICKSYNCCONTEXT`、`RGXKICKSYNC2` |
| 辅助 | 16 | DI 5 / SYNCTRACKING 2 / DEVICEMEMHISTORY 5 / CMM 3 / MMEXTMEM 1 | 调试/历史/包装，非首像素必需 |

UM 的初始化调用序列（按组与函数名推断，具体顺序由 UM 二进制决定）：
`SRVCORE_CONNECT` → `ACQUIREINFOPAGE` / `GETDEVICESTATUS` →
`MM_HEAPCFG*` / `GETDEFAULTPHYSICALHEAP` → `PHYSMEMNEWRAMBACKEDPMR` →
`DEVMEMINTCTXCREATE` / `DEVMEMINTHEAPCREATE` / `DEVMEMINTMAPPMR` →
`RGXTA3D_CREATERENDERCONTEXT` → `RGXTA3D_RGXKICKTA3D2` →
`SYNC_ALLOCSYNCPRIMITIVEBLOCK` / `SYNCPRIMSET`（或 event object）→
`CACHE_CACHEOPQUEUE/EXEC`。

### 1.3 mmap 区域

- 只有一个 mmap 入口：`PVRSRV_MMap`（`pvr_bridge_k.c:571`），挂在
  `pvr_drm_fops.mmap`。
- `vm_pgoff` **不是字节偏移，而是 secure PMR handle**：
  `hSecurePMRHandle = (IMG_HANDLE)(uintptr_t)ps_vma->vm_pgoff`；
  内核用 `PVRSRVLookupHandle(PVRSRV_HANDLE_TYPE_PHYSMEM_PMR)` 找到 PMR，
  再 `PMRMMapPMR`。
- `PROT_READ/WRITE` 映射为 `PVRSRV_MEMALLOCFLAG_CPU_READABLE/WRITEABLE`；
  只读映射会清 `VM_MAYWRITE`。
- 支持的 PMR 后端（`services/server/env/linux/`）：OS memory
  (`physmem_osmem_linux.c`)、dma-buf (`physmem_dmabuf.c`)、外部内存
  (`physmem_extmem_linux.c`)、wrap extmem (`physmem_extmem_wrap.h`)。

### 1.4 dma-buf / PRIME

- DRM 驱动声明 `DRIVER_PRIME`；导出走
  `drm_gem_prime_handle_to_fd` + `PhysmemGEMPrimeExport`
  （`physmem_dmabuf.c:1933`）。
- `prime_fd_to_handle` **明确不支持**（`pvr_drm.c:546` 注释）；导入走
  DMABUF bridge 的 `PHYSMEMIMPORTDMABUF`/`PHYSMEMIMPORTSPARSEDMABUF`
  （4 条命令里的 2 条）。
- dma-buf ops 实现 `mmap`、`map/unmap_dma_buf`、
  `begin/end_cpu_access`（即 `DMA_BUF_IOCTL_SYNC` 语义）、`release`
  （`physmem_dmabuf.c:763` 与 `:930` 两套：非 GEM 与 GEM）。

### 1.5 sync / fence

- 6 条 PVR sync DRM ioctl（上表 #2–#5、#7–#8）+ 10 条 SYNC bridge 命令。
- 语义：Android `sw_sync` 风格时间线（create fence/inc）；
  `dma_fence` 导出（`pvr_export_fence.c`，`dma_fence_ops`、`dma_fence_init`、
  `dma_fence_signal`）；`SUPPORT_BUFFER_SYNC` 另有一套 buffer sync。
- `pvr_sync_ioctl_drm.c` 是 DRM ioctl 的实现层；
  `pvr_sync_api.h` 是 UM/KM 的同步 API 面。

## 2. Asterinas 现状对照

状态口径：**已有** = 可直接复用；**部分** = 有同类机制但语义/接口不匹配；
**缺** = 没有对应实现。

| UAPI 项 | Asterinas 现状 | 需要的内核改动（一句一项） |
|---|---|---|
| `card0`/`renderD128` 节点 | 部分：`DriNode::{Card,Render}` 已存在，但只在 `display_source()` 非空时暴露，后端绑定 virtio-gpu/固件 framebuffer | 让 PVR 后端注册为一个 DRM source，并按 `DRM_RENDER_ALLOW` 扩展 render 白名单 |
| DRM core ioctl（VERSION/GET_CAP/SET_CLIENT_CAP/GET_MAGIC/AUTH_MAGIC/SET_MASTER） | 已有（`ioctl_defs` + 分发） | 无 |
| PVR 的 8 条 DRM ioctl | 缺 | 增加 `DRM_IOCTL_PVR_SRVKM_CMD/SRVKM_INIT` 与 6 条 sync ioctl 的编码、分发和白名单 |
| Bridge 分发（`bridge_id`/`func_id` + 用户指针） | 缺 | 实现带大小校验的 `copy_from_user`/`copy_to_user` 桥，并建立最小函数表 |
| GEM 对象创建/关闭/命名 | 部分：`GEM_CLOSE/FLINK/OPEN` 已有；创建走 virtgpu `RESOURCE_CREATE`/dumb | 为 PVR 增加一条“PMR→GEM handle”的创建路径（bridge 或专用 ioctl） |
| GEM mmap | 部分：现有 `Mappable::Vmo(pool)` 用池偏移 mmap | 增加按 secure PMR handle（`vm_pgoff`）查找并映射的 mmap 路径 |
| PRIME 导出 | 部分：Asterinas 自己的 GEM→`DmaBufFile` 已可用 | 把 PVR PMR/GEM 接到现有 `prime::handle_to_fd` 或新的 dma-buf 导出 |
| PRIME 导入 | 部分：Asterinas 有 `PRIME_FD_TO_HANDLE`；vendor 实际走 DMABUF bridge | 实现 4 条 DMABUF bridge 命令，接受外部 dma-buf fd |
| dma-buf `DMA_BUF_IOCTL_SYNC` | 缺（`DmaBufFile` 无该 ioctl） | 在 dma-buf 文件上实现 begin/end_cpu_access ioctl 与 cache 维护 |
| sync prim / event object（SRVCORE/SYNC bridge） | 缺 | 实现可阻塞的 event object（wait/timeout）并接入 poll/wake |
| sw_sync 时间线 / export dma_fence | 缺：只有 `fence.rs` 的“创建即完成”占位 | 实现时间线、可等待 fd 和 `dma_fence` 等价物 |
| DRM syncobj（0xBF–0xCF） | 缺 | 若 P3 选择通用路径，补 syncobj/timeline ioctl；否则由 PVR sync 覆盖 |
| GPU IRQ 交付 | 部分：P2.4 的 handler 只在 probe 内计数 | 提供 IRQ→用户态服务/工作队列的交付路径（A 案）或内核 job 完成队列（B 案） |
| KMS/simple-scanout | 已有：GETRESOURCES/GETCRTC/SETCRTC/GETENCODER/GETCONNECTOR/ADDFB/RMFB/DIRTYFB/CREATE|MAP|DESTROY_DUMB/GETPLANE 等 | 无（异步 page-flip 事件缺失，见下） |
| page-flip 完成事件 | 部分：PAGE_FLIP 同步执行，拒绝 EVENT/ASYNC 标志，不排队 flip 事件 | 增加 flip 完成事件队列 + poll 唤醒（合成器/glamor vsync 路径） |
| Cache op bridge（CACHE 3 条） | 缺 | 在 DMA/PMR 缓冲上实现 CPU cache clean/invalidate 的语义与 ioctl |
| fdinfo（PVR 利用率） | 缺 | 可选；`SUPPORT_LINUX_FDINFO` 的键不影响首像素 |
| 固件 MMU/渲染上下文/作业提交 | 部分：P2 只有固件启动、MMU 表、KCCB HEALTH_CHECK | 增加渲染上下文、freelist、作业 kick 与完成路径 |

## 3. DRM render node 最小 UAPI（P4/P6 预期）

### 3.1 render node 核心（全部 `DRM_RENDER_ALLOW`）

| ioctl | nr | 最小用途 | Asterinas |
|---|---|---|---|
| `DRM_IOCTL_VERSION` | 0x00 | 识别驱动 | 已有 |
| `DRM_IOCTL_GET_CAP` | 0x0c | 能力位 | 已有（需 PVR 能力位） |
| `DRM_IOCTL_SET_CLIENT_CAP` | 0x0d | 客户端能力协商 | 已有 |
| `DRM_IOCTL_GEM_CLOSE` | 0x09 | 释放 handle | 已有 |
| `DRM_IOCTL_PRIME_HANDLE_TO_FD` | 0x2d | 导出 dma-buf | 已有 |
| `DRM_IOCTL_PRIME_FD_TO_HANDLE` | 0x2e | 导入 dma-buf | 已有（vendor 走 bridge，见 1.4） |
| `DRM_IOCTL_PVR_SRVKM_CMD` | 0x40 | 所有 PVR 操作 | 缺 |
| `DRM_IOCTL_PVR_SRVKM_INIT` | 0x45 | 首开初始化 | 缺 |
| 6 条 PVR sync ioctl | 0x41–0x47 | 时间线/fence | 缺 |

### 3.2 GEM 对象创建/映射（二选一，P4 决策点）

- **路径 V（vendor 原生）**：bridge
  `MM_PHYSMEMNEWRAMBACKEDPMR` → `MM_DEVMEMINTMAPPMR` 返回 secure PMR
  handle；用户态 `mmap(fd, len, prot, MAP_SHARED, offset=handle)`；
  内核按 1.3 的语义查找 PMR。
- **路径 A（复用 Asterinas 池）**：bridge 创建对象时返回池内偏移；
  现有 `Mappable::Vmo(pool)` + `MODE_MAP_DUMB` 风格偏移直接复用；
  需要把 PVR PMR 的物理页固定到池或做窗口映射。

最小验收（P4）：一个 4 KiB 缓冲经上述路径创建、mmap、CPU 写入、
GPU 侧可读（或反向），并能在 `GEM_CLOSE` 后回收。

### 3.3 fence 最小集

- PVR 路径：`SW_SYNC_CREATE_FENCE`（0x43）+ `SW_SYNC_INC`（0x44）+
  `SYNC_CREATE_EXPORT_FENCE`（0x47）；等待方用 `poll`。
- 通用路径（若走 DRM syncobj）：`SYNCOBJ_CREATE/WAIT/TIMELINE_WAIT`
  （0xBF/0xC3/0xCA）；Asterinas 目前都没有。
- 最小验收：一个 kick 完成后 fence 可被 `poll(POLLIN)` 观察到，
  且时间线可推进/重置。

### 3.4 KMS / simple-scanout 最小集（P6 预期）

| ioctl | nr | 用途 | Asterinas |
|---|---|---|---|
| `MODE_GETRESOURCES` | 0xa0 | 枚举 CRTC/connector | 已有 |
| `MODE_GETCRTC` | 0xa1 | 读当前模式 | 已有 |
| `MODE_SETCRTC` | 0xa2 | 设模式/扫描缓冲 | 已有 |
| `MODE_GETENCODER` | 0xa6 | 连接关系 | 已有 |
| `MODE_GETCONNECTOR` | 0xa7 | 连接器/模式列表 | 已有 |
| `MODE_ADDFB` | 0xae | 注册 framebuffer | 已有 |
| `MODE_RMFB` | 0xaf | 删除 framebuffer | 已有（raw 0xc00464af） |
| `MODE_PAGE_FLIP` | 0xb0 | 翻页 | 部分（同步；无 EVENT 队列） |
| `MODE_DIRTYFB` | 0xb1 | 脏区 | 已有 |
| `MODE_CREATE_DUMB` | 0xb2 | 创建扫描缓冲 | 已有 |
| `MODE_MAP_DUMB` | 0xb3 | 取 mmap 偏移 | 已有 |
| `MODE_DESTROY_DUMB` | 0xb4 | 销毁 | 已有 |

P6 最小验收：`modetest`/glamor 能完成一次
`CREATE_DUMB → MAP_DUMB → ADDFB → SETCRTC → PAGE_FLIP`，
且 Firefox 窗口内容出现在扫描输出上。

## 4. A/B 两案的第一批 bounded 任务

### 方案 A（厂商 C 核心 + Rust OS/DRM 层）

| # | 任务 | 验收物 |
|---|---|---|
| A1 | GPU 服务进程骨架：用户态进程持有 P2 的 probe 设备与共享环，客户端可提交一条 `HEALTH_CHECK` | 服务日志 `FW_KCCB status=completed`；客户端往返测试；内核无 unsafe C |
| A2 | C services 最小移植：`SRVCORE_CONNECT`、`PHYSMEMNEWRAMBACKEDPMR`、`DEVMEMINTMAPPMR` 在 Asterinas 上跑通（RAM PMR + mmap） | C 测试二进制成功 alloc/map；读写校验；启动日志 |
| A3 | Bridge 前端：内核实现 `DRM_IOCTL_PVR_SRVKM_CMD` 到服务的转发（in/out 用户缓冲安全拷贝、大小校验、最小函数表） | 无 libdrm 的 C 测试发起 bridge 调用；非法长度返回 `EINVAL`；往返时延记录 |
| A4 | UM DDK 接入：`libsrv_um`/`libusc` 与该前端链接，完成连接、分配、创建渲染上下文 | UM 日志显示 connect/alloc/render context 成功；首次 GLES clear 或等价 KCCB kick |
| A5 | DRM render 节点 + 显示路径：PVR 后端暴露 `renderD128`，PRIME 导出，接入现有 KMS/fbdev 扫描输出 | `renderD128` 可见；PRIME 导入导出往返；Xorg/glamor 绑定 PVR 的收据 |

### 方案 B（纯 Rust 重写）

| # | 任务 | 验收物 |
|---|---|---|
| B1 | KCCB/FWCCB + IRQ 完整化：return-slot、FWCCB 消息、IRQ 驱动的完成队列 | 一条 KICK 命令 return slot 置位；FWCCB 消息被消费；ktest + 板级 marker |
| B2 | 固件 MMU 上下文与堆：渲染上下文页表、GPU VA 分配、PMR 等价缓冲映射 | 缓冲映射进上下文后，固件作业可读写（模式校验）；板级 marker |
| B3 | 渲染上下文与作业提交：create/destroy、TA/3D 或 transfer job、完成 fence | 一个最小作业完成；fence 可观察；ktest |
| B4 | DRM UAPI + GEM/PRIME/sync：renderD128、GEM create/mmap、PRIME、syncobj 或 PVR sw_sync | libdrm 风格测试完成 map/export/wait；`modetest`/自写测试通过 |
| B5 | Mesa/GLES 后端与 Firefox：为 vendor 固件 UAPI 写/改 Mesa 后端，跑通 GLES 与 Firefox | GLES clear/triangle 收据；Firefox 硬件加速收据（E3 场景） |

## 5. 厂商源码留档风险

`/tmp/eswin-src` 在 `/tmp`，重启即丢。已记录可重建信息，并把文件清单
sha256 落到未跟踪证据目录：

- 来源：`https://github.com/eswincomputing/linux-stable.git`
- 分支：`linux-6.6.18-EIC7X`
- HEAD：`fc6038c00e006226e3bd504d2679c534eabf5503`
  （2026-07-13，“Remove deprecated dts and dtsi files”）
- 稀疏路径：`drivers/gpu/drm/img/img-volcanic`（879 文件）、
  `arch/riscv/boot/dts/eswin`（116 文件）、`include/uapi/drm`（25 文件）
- 清单：`target/p3-uapi-inventory-20261022/vendor-manifest.sha256`
  （1020 行），清单自身 SHA-256
  `5cd196c9161a4074465cea404a23332870f61349e0fb8b3caaf2128cd9857919`；
  来源信息见同目录 `vendor-provenance.txt`。
- 重建命令：

```bash
git clone --depth 1 --filter=blob:none --sparse \
  --branch linux-6.6.18-EIC7X \
  https://github.com/eswincomputing/linux-stable.git eswin
cd eswin
git sparse-checkout set drivers/gpu/drm/img/img-volcanic \
  arch/riscv/boot/dts/eswin include/uapi/drm
git rev-parse HEAD   # 快照时为 fc6038c00e006226e3bd504d2679c534eabf5503
find drivers/gpu/drm/img/img-volcanic arch/riscv/boot/dts/eswin include/uapi/drm \
  -type f -print0 | sort -z | xargs -0 sha256sum > vendor-manifest.sha256
sha256sum vendor-manifest.sha256  # 应为 5cd196c9…
```

若分支 HEAD 已前移，用
`git fetch --depth 1 origin fc6038c00e006226e3bd504d2679c534eabf5503 &&
git checkout fc6038c00e006226e3bd504d2679c534eabf5503` 取回快照 commit；
清单 sha256 仍是内容一致性的最终判据。

- 同一份清单与 UAPI 证据（本文件）入库；原始 clone 不入库。
- 若上游 force-push 或分支消失，HEAD commit 仍可按 SHA 获取
  （GitHub 允许按 commit 取归档）；若连 commit 也消失，本清单可用于
  校验手头副本的完整性。
