# x86 / RISC-V 共享 DRM 结构设计

状态：设计提案（2026-10-21）。目标是把 x86 线与 RISC-V 线已经积累的 DRM
能力收敛到**一个核心 + 一组可插拔驱动**，而不是继续维护互不兼容的两套
`/dev/dri` 实现。

## 0. 结论

- **唯一核心选 live 的 `aster-drm`**（`kernel/comps/drm`）。它已经有设备/
  ioctl 分发、GEM（dumb/shmem）、传统 KMS 和 render node 权限模型，驱动接口
  很窄（`DrmDevice` / `DrmGemOps` / `DrmKmsOps`），适合作为共享层。
- **驱动插件化**：simpledrm、virtio-gpu、firmware-fb、EIC7700/PowerVR 都只
  实现同一组 trait，不各自注册字符设备、不各自实现 ioctl 前端。
- **加一层 `aster-drm-host` 薄适配**：把 core 对 `aster-core` 的少量依赖
  （char 注册、mmap 对象、credentials、ioctl 分发）抽成 trait，使同一份 DRM
  core 既能编进本仓库的 `aster-core`，也能编进 RISC-V 线使用的 upstream
  布局内核。RISC-V 线不必立刻迁到 `aster-core`。
- **第一个跨架构里程碑是 virtio-gpu 2D 驱动**：同一份驱动 crate 先在 x86
  QEMU 上把 Xorg modesetting 跑起来，再原样在 RISC-V QEMU 上跑一遍——这就是
  “两块 DRM 一起用”的最小可验收闭环。
- RISC-V 的两条线（当前线 `dri.rs`、完整线 `device/drm/`）不再作为长期核心；
  **通用能力按功能移植进 `aster-drm`，硬件相关部分变成驱动**，来源 commit
  在每次移植时记录。

## 1. 现状与证据

| | x86 live 树（本仓库） | RISC-V 当前线 | RISC-V 完整线 |
|---|---|---|---|
| 代码位置 | `kernel/comps/drm` + `kernel/comps/simpledrm` | `kernel/src/device/dri.rs` + 4 模块 | `kernel/src/device/drm/`（22 文件） |
| 内核基础 | `aster-core`（`kernel/core`） | upstream 布局（`kernel/src`） | upstream 布局 |
| ioctl | 传统 KMS + dumb/GEM + 部分 capability | 37 | 49（含 atomic/同步/vblank） |
| 独有能力 | 干净的 crate 边界、驱动 trait、render node 权限 | virtio-gpu 2D+virgl、PRIME 基础、firmware scanout backend | atomic KMS、property/blob、syncobj（12）、vblank |
| 已验证 | simpledrm 需要 bootloader framebuffer；x86 当前无任何 GPU 驱动 | QEMU virtio-gpu + virgl 跑起 Debian 桌面（Xorg modesetting，renderer=virgl） | kmscube 全链路（GBM→EGL→ADDFB2→atomic flip） |
| 真机 | — | Megrez/EIC7700 的 `/dev/dri/card0` 从未被打开，桌面走 Xorg fbdev | 同左 |

关键约束（已实测）：

1. x86 现在用 QEMU `-kernel`（multiboot2）直启；QEMU 的 multiboot loader
   **不生成 framebuffer tag**（`-vga std`、`-vnc :9` 都试过），所以
   `simpledrm` 在 x86 上报 `boot framebuffer is unavailable`。
2. live 内核里 `VirtioDeviceType::Gpu` 只落到 “unimplemented”，没有任何
   GPU 驱动；`aster-drm` 的驱动接口也还没有 atomic/PRIME/syncobj 的钩子。
3. RISC-V 的两条线与 x86 live 树**不是同一个内核基础**：前者的代码以
   `kernel/src` 为活内核，在本仓库里那棵树已经不再参与编译。

## 2. 目标结构

```
userspace: Xorg (fbdev/modesetting) · Mesa/GBM · libdrm probes
                         │  /dev/dri/cardN, /dev/dri/renderDN, dma-buf fds
┌────────────────────────▼─────────────────────────────────────────────┐
│ aster-drm（唯一 core；拥有 DRM char 设备与全部 UAPI）                 │
│  file/ioctl dispatch · object store · permissions · render node        │
│  ├─ gem: dumb/shmem · mmap · flink/open                                │
│  ├─ kms: legacy（SETCRTC/PAGE_FLIP/DIRTYFB）→ atomic（后续）           │
│  ├─ prime: dma-buf export/import + DmaBufFile + SCM_RIGHTS 审计        │
│  └─ sync: syncobj / fence / vblank（后续）                             │
└───────────────▲───────────────────────────────▲───────────────────────┘
    driver traits: DrmDevice · DrmGemOps · DrmKmsOps · DrmPrimeOps* · DrmSyncOps*
┌───────────────┴───────┐   ┌───────────────────┴──────────┐
│ drm-virtio-gpu        │   │ drm-eic7700 (RISC-V hardware) │
│ (x86 + RISC-V QEMU)   │   │ firmware/PowerVR, 后续        │
└───────────────▲───────┘   └───────────────────────────────┘
                │  virtio-gpu 2D（先）/ virgl（后）
┌───────────────┴─────────────────────────────────────────────┐
│ kernel/core/comps/virtio  (transport, protocol, queues)     │
└─────────────────────────────────────────────────────────────┘

              ┌──────────────────────────────┐
              │ aster-drm-host (薄适配层)     │
              │ char registry · mmap/mapped   │
              │ object · credentials · ioctl  │
              │ dispatch · logging            │
              └───────▲──────────────▲────────┘
          aster-core impl     upstream-layout impl（RISC-V 线提供）
```

`*` 为新增 trait；driver 按 feature 位声明支持能力，core 对未声明的 ioctl
返回 `EOPNOTSUPP`。

## 3. 结构决策

- **D1 单一节点所有权**：`aster-drm` 注册 `cardN`/`renderDN`；驱动只拿到
  “设备已注册”的回调，禁止再注册自己的 `/dev/dri`。这样两套实现不会在同一
  内核里抢同一个 major，也避免用户在两个 ABI 之间二选一。
- **D2 驱动插件化**：驱动 crate 仅依赖 `aster-drm` 的 trait 与 `ostd`；硬件
  相关的寄存器/协议留在驱动内。virtio-gpu 的协议层放在 live
  `kernel/core/comps/virtio`，DRM 适配放在 `kernel/comps/drm-virtio-gpu`。
- **D3 内核基础解耦**：`aster-drm` 目前直接依赖 `aster_core` 的 char 注册、
  `MappedObject`/`MapHandle`、credentials、`dispatch_ioctl!`。把这些收敛到
  `aster-drm-host` trait：
  - 本仓库提供 `aster-core` 实现（默认 feature），行为不变；
  - RISC-V 线提供它自己的实现，就能直接复用同一个 `aster-drm`；
  - 长期若 RISC-V 线愿意迁到 `aster-core`，删掉适配实现即可。
- **D4 能力分层**：core 按 `DrmFeatures` 暴露能力，驱动可以只实现
  legacy KMS；atomic/PRIME/syncobj 作为可选 trait，按阶段加。
- **D5 迁移而不是并存**：RISC-V 当前线的 `ScanoutBackend`/`CursorBackend`
  由 `DrmKmsOps` 取代；完整线的通用 ioctl 语义移植进 `aster-drm` 后，原实现
  退役。任何“两个 /dev/dri 同时存在”的方案都不采纳。

## 4. 代码落点（移植映射）

| 来源 | 去向 | 动作 |
|---|---|---|
| `aster-drm`（live） | 保持不变，作为 core | 扩展 trait 与能力模块 |
| `kernel/src/device/dri/backend.rs` 的 `ScanoutBackend`/`CursorBackend`/firmware backend | `aster-drm::kms` + simpledrm/firmware 驱动 | 语义映射到 `DrmKmsOps`；firmware backend 变成 simpledrm 的 present 路径 |
| `kernel/src/device/dri/prime.rs` + `kernel/src/net/socket/unix/ctrl_msg.rs` 的 proven-leaf 审计 | `aster-drm::prime` + live Unix socket SCM_RIGHTS | 移植 dma-buf 文件对象与审计层（live 当前没有该审计层） |
| `kernel/src/device/dri/fence.rs` | `aster-drm::sync` | 与完整线的 syncobj 一起做 |
| 完整线 `kernel/src/device/drm/` 的 atomic/property/blob/vblank | `aster-drm::kms::atomic` | 按功能移植，记录来源 commit |
| `kernel/comps/virtio/src/device/gpu`（死树副本，~600 行 2D 协议 + 3D virgl） | `kernel/core/comps/virtio/src/device/gpu` | 协议/队列代码先搬 2D 部分；3D 后续 |
| 新 `kernel/comps/drm-virtio-gpu` | 驱动 crate | 实现 DrmDevice/Gem/Kms（+后续 Prime/Sync） |
| `eic7700.rs` / `powervr_probe/*` | RISC-V 驱动的 `drm-eic7700` | 硬件相关，留在 RISC-V 线，后续适配 |

## 5. 阶段与验收

- **P0（本设计）**：结构、能力矩阵、trait 草案与 conformance 清单入库；
  建立“同一驱动在两架构跑同一组检查”的验收模板。
- **P1 virtio-gpu 2D（第一个跨架构闭环）**：
  - live virtio 增加 GPU control queue 与 2D 命令（display info、resource
    create/attach backing、set scanout、transfer、flush、cursor）；
  - `drm-virtio-gpu` 实现 dumb buffer、connector/CRTC、`set_crtc`、
    `refresh_dirty_fb`；
  - 验收：x86 QEMU `-device virtio-gpu-pci` 下 simpledrm 不再需要 boot
    framebuffer，Xorg modesetting 起来，`screendump` 非黑；随后同一驱动在
    RISC-V QEMU 上重复同一脚本。
- **P2 PRIME / dma-buf**：`DmaBufFile`、SCM_RIGHTS proven-leaf、导出/导入；
  验收：X DRI3 把 DRM fd 与 dma-buf 经 socket 交给 Xorg，渲染后扫描出画面；
  fd 传递的正负用例（单进程、跨进程、错误类型 EPERM）。
- **P3 atomic KMS + syncobj/vblank**：从完整线移植；验收：modetest 原子
  提交、syncobj 12 个 ioctl 的语义测试、kmscube（可选 3D 前提）。
- **P4 桌面产品化**：amd64 desktop profile（Xorg + matchbox + xterm）与
  RISC-V desktop 共用同一套 DRM 驱动；新增 graphical daily-use gate（登录 →
  起 X → 起应用 → 截图 → 电源键关机）。
- **P5 退役**：RISC-V 当前线的 `dri.rs`/`ScanoutBackend` 删除；完整线只作为
  移植来源；CI 跑两架构的同一 conformance 矩阵。

## 6. 风险与开放问题

- **内核基础收敛方式**：短期用 `aster-drm-host` 适配层让两边共用 core；长期
  仍建议 RISC-V 线迁到 `aster-core`，否则适配层要跟着 upstream 内核 API 漂移。
- **完整线是 rollup**（PR #139 未按层评审）：只按功能移植，避免整体 cherry-pick。
- **virgl/3D 不在 P1**：桌面 2D modesetting 不依赖 virgl；Mesa 的 GL 渲染要等
  P3 之后，届时 virtio-gpu 3D 与 syncobj 一起做。
- **x86 无 GPU 时的 fallback**：QEMU multiboot 不给 framebuffer tag，若要
  “无 virtio-gpu 也能出画面”，需要 GRUB 启动（framebuffer tag）或一个最小
  Bochs VBE 驱动；这条不在 virtio-gpu 主线，但应作为 P1 的独立 spike。
- **Conformance 工具**：优先用 Debian 自带的 libdrm 探针/`modetest`（amd64
  原生安装；RISC-V 用交叉包），必要时写一个只依赖 libdrm 的小 C 探针，作为
  两架构共用验收。
