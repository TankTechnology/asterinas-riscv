# #203（DRM 大 PR）retarget 到 main 的拆分计划

日期：2026-10-28
分析基线：`origin/main` = `810b0c6e4`（#221 合并后），
`origin/feat/drm-virtio-gpu` = `e9216a061`（#203 的 head）
合并基点：`39a57d275`

## 1. 结论

1. **#203 不能整体合 main，也不能直接 cherry-pick**。三点 diff 是
   2079 个文件 / +278145 / −24289，但其中 **1890 个文件、约 24 万行是布局与
   基线漂移**：`kernel/src` → `kernel/core` 的重构（779+215 文件）、ostd
   195 文件、test/initramfs 228 文件、book/osdk/distro/.agents 等。这些是
   feat 分支从旧基点带来的，不是 DRM 功能，必须剥离。
2. DRM 本体约 **189 个文件 / +3.7 万行**，可以按四类拆：
   ① shared-core virtio-gpu（31 文件 / +7287）② PRIME/dmabuf（5 / +965）
   ③ atomic KMS + page flip + vblank（20 / +3547）④ Xorg 门禁与文档
   （133 / +25178 / −3063，含探针、脚本、测试和文档）。
3. **③ 内部还能再拆**：`atomic KMS` 依赖 ① 的 KMS 对象/属性模型
   （property/plane/crtc 的 atomic state），无法在 main 的
   `kernel/src/device/dri.rs` 单体实现里独立完成；`page flip 完成事件 +
   vblank` 只依赖 main 已有的 KMS 枚举/`SETCRTC`/`PAGE_FLIP` 与文件事件
   基础设施，可以先独立落地。
4. **验证条件**：main 的内核目前不能引导 Debian 镜像（无可挂载 devtmpfs、
   不接 `--root-init=systemd`，见 `2026-10-28-sem-undo.md` §3），而
   `modetest`/`xrandr` 需要 Debian + X。main 上的 DRM 验证改用**最小
   initramfs 里的 raw-ioctl 探针**（GETRESOURCES/GETCONNECTOR/GETCRTC、
   dumb buffer、SETCRTC、PAGE_FLIP+EVENT、WAIT_VBLANK）；等 ① 或
   Debian 支持回 main 后再补 Debian+X 的矩阵。

## 2. 四类改动归档

行数为三点 diff 的 numstat；文件路径相对仓库根。

### ① shared-core virtio-gpu（31 文件，+7287）

- `kernel/comps/drm/**`（30 文件）：设备模型、GEM、KMS 对象
  （connector/encoder/crtc/framebuffer）、ioctl 框架
  （general/gem/kms）、`device.rs`、`file.rs`、`minor.rs`。
- `kernel/comps/drm-virtio-gpu/**`（2 文件）：virtio-gpu 驱动插件。
- `kernel/comps/simpledrm/**`（2 文件）：固件 framebuffer 后端。
- 关联但未在此计数：`kernel/core/src/device/drm_sysfs.rs`、
  `kernel/core/comps/virtio/src/device/gpu/**`（驱动侧），
  `kernel/core/comps/framebuffer/**` 的使用调整。

**依赖**：ostd、kernel 的设备注册/`Devtmpfs`、`aster-framebuffer`、事件
（`kernel/core/src/events`）。**能否独立合 main**：原理上可以（它是一个
新的组件 crate + 把 `kernel/src/device/dri.rs` 换成组件实现），但不是
cherry-pick，是把 ①+③ 的代码整体重写到 main 的 `kernel/src` 布局；
改动量最大。**冲突点**：main 的 `kernel/src/device/dri.rs` 已有同名功能，
替换时需决定「组件化」还是「继续单体」。

### ② PRIME / dmabuf（5 文件，+965）

- `kernel/comps/drm/src/prime.rs`、`gem/shmem.rs`、`sync_file.rs`、
  `syncobj.rs`、`ioctl/syncobj.rs`
- 关联：`kernel/core/src/fs/file/anon.rs`（导出 dma-buf fd）。

**依赖**：① 的 GEM 对象与文件模型；**能否独立合 main**：否（GEM/dma-buf
都挂在 ① 的对象上）。**冲突点**：main 已有 `kernel/src/device/dri/prime.rs`
的早期实现，需要对齐语义（fd 类型、mmap、SCM_RIGHTS）。

### ③ atomic KMS + page flip + vblank（20 文件，+3547）

- `kernel/comps/drm/src/ioctl/atomic.rs`（473）、`ioctl/flip.rs`（99）、
  `flip.rs`（195）、`event.rs`（62）
- `kernel/comps/drm/src/kms/objects/property/{mod,blob,in_formats}.rs`
  （461）、`objects/plane.rs`（144）、`objects/crtc.rs`（254）
- `kernel/core/src/events/event_source.rs`（+86）、`time/wait.rs`（+38）、
  `time/mod.rs` 等
- `tools/drm/{atomic_probe.c,flip_probe.c,multi_crtc_probe.c}`（④ 类，
  但直接验证 ③）

**依赖**：atomic 一半强依赖 ① 的 KMS 对象/属性模型与 ③ 的 plane/crtc
state；flip/vblank 一半只依赖「文件事件队列 + 同步 present + vblank
计数」。**能否独立合 main**：flip/vblank 可以；atomic 不可以。
**冲突点**：feat 的 flip 是异步 worker（vblank tick 驱动），main 的
`present_fb` 是同步的；main 的文件 `poll` 已有 `Pollee` 但从未置位；
`WAIT_VBLANK` ioctl 在 main 上完全缺失。

### ④ Xorg 门禁 / 脚本 / 文档（133 文件，+25178 / −3063）

- `tools/x86/debian/**`（31）：Xorg/DRI3/键盘/输入法门禁与探针
- `tools/drm/**`、`test/initramfs/src/regression/device/drm/**`：C 探针
- `tools/riscv/**`、`.github/workflows/**`、`docs/porting/**`（25 篇）

**依赖**：需要 ①/③ 的可运行镜像；**能否独立合 main**：文档可以先合，
门禁/探针要等对应代码。**冲突点**：这些脚本引用 feat 分支的
`kernel/core` 路径与构建参数。

## 3. 拆分顺序建议

原任务写的顺序是 ③→①→②。按依赖关系调整为：

1. **③a page flip 完成事件 + vblank**（本次 PR）：落在 main 的
   `kernel/src/device/dri.rs`，不引入组件模型，可独立验证。
2. **① shared-core**：把 `kernel/comps/drm` + 插件迁到 main（需要把
   代码从 feat 的 `kernel/core` API 适配到 main 的 `kernel/src`）。
3. **③b atomic KMS**：建立在 ① 的对象/属性模型上，与 feat 的
   `ioctl/atomic.rs` + `kms/objects/property/**` 对齐。
4. **② PRIME/dmabuf**：依赖 ① 的 GEM；若 main 现有 `dri/prime.rs` 已够用，
   只需补 dma-buf 的 fd 语义。
5. **④ 门禁与文档**：随 ①/③b 的落地分批合，避免一次性带入 25178 行。

## 4. 本次 PR（③a）的范围

- `kernel/src/device/dri.rs`：
  - 每个打开文件一个 DRM 事件队列；`poll` 在有事件时可读，`read`
    按 `drm_event` 记录出队（`DRM_EVENT_FLIP_COMPLETE`，36 字节）。
  - `MODE_PAGE_FLIP` 接受 `EVENT|ASYNC`；present 成功后入队完成事件并唤醒
    `Pollee`；未请求事件时不入队。
  - `DRM_IOCTL_WAIT_VBLANK`：每 CRTC 的 vblank 序号/时间戳在每次 present
    时推进；支持相对/绝对等待，目标未到时在等待队列上阻塞，present 唤醒。
  - 单测：flag 校验、事件编码、vblank 推进与等待算术。
- **不包含**：`DRM_CLIENT_CAP_ATOMIC`、`MODE_ATOMIC`、KMS 属性/blob、
  异步 flip worker、PRIME。它们在 PR 说明里标注为依赖 ① 的后续工作。
- 验证：`cargo osdk build --target-arch x86_64` 通过；x86 QEMU + 最小
  initramfs 的 raw-ioctl 探针能列 mode、做一次 `PAGE_FLIP`（带事件并读到
  完成事件）、`WAIT_VBLANK` 返回推进后的序号且不崩。

## 5. 验证结果（2026-10-28，③a）

编译（宿主 cargo-osdk 被另一个 worktree 以 `OSDK_LOCAL_DEV=1` 编译，路径烤死；
这里用本仓库源码重建 OSDK 后编译）：

```
cargo osdk build --target-arch x86_64            # grub 打包因宿主机无 grub-mkrescue 停在打包步
cargo osdk build --target-arch x86_64 --scheme microvm --for-test   # 通过（含 ktest 单测代码）
```

x86 QEMU（`-device virtio-gpu-pci`，main 内核 `aster-kernel-osdk-bin.qemu_elf`，
探针 `tools/x86/drm/flip_vblank_probe.c` 作为最小 initramfs 的 `/init`）：

```
DRM_PROBE_START
DRM_PROBE_RES connectors=1 crtcs=1 encoders=1 fbs=0
DRM_PROBE_MODES count=1 connection=1 first=1280x800 1280x800
DRM_PROBE_CRTC id=1 fb=0 mode_valid=1 1280x800
DRM_PROBE_FB fb=1 handle=1 size=1228800 pitch=2560
DRM_PROBE_FLIP_EVENT type=2 len=32 user=0x5eed
DRM_PROBE_VBLANK sequence=2 tv=1.122301
DRM_PROBE_OK
```

即：能列出 mode；带 `PAGE_FLIP_EVENT` 的翻转成功并读回
`DRM_EVENT_FLIP_COMPLETE`（type=2、len=32、user_data 原样）；不带事件的翻转
不再留下事件（`read` 返回 EAGAIN）；`WAIT_VBLANK` 返回随两次 present 推进到 2
的序号与时间戳，整个过程中无 panic。

说明：任务原本要求 `modetest`/`xrandr`。main 的内核不能引导 Debian 镜像
（§1），X 和 libdrm 的 modetest 无法在 main 上运行，因此用同一 UAPI 的
raw-ioctl 探针完成等价验收；等 Debian 支持（或 ①）回 main 后再补 modetest /
xrandr 的真实矩阵。
