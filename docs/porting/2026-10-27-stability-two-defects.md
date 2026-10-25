# 两个稳定性缺陷：task-exit panic 与 DRM 挂死

日期：2026-10-26/27
内核：修复前 `923ca5c53ea46e02`；A 单独验证 `3da2169fdf1b25c1`；
最终 A+B `f3299cd564cece11`
镜像：`toolchain4`（2.1 GiB，7 域矩阵）与 `ff-lag`（含 firefox/x11-apps）

## 结论摘要

- **缺陷 A（panic）已修**：`ostd::exit_current()` 末尾的 `unreachable!()`
  改为**幂等重试退出**；新增 ktest `concurrent_task_exit`（64 个任务并发退出）。
  回归：200 次 `xclock`（无 X server 立即退出，即原始触发场景）+ 3000 次
  fork/exec + 2000 次线程退出 → **panic 0**，矩阵 7 域 panic 0。
- **缺陷 B（挂死）做了内核侧加固，但根因不在内核等待路径**：
  - `0xc01064ab` 不是 vendor bridge，而是 **`DRM_IOCTL_MODE_SETPROPERTY`**
    （`0xab` 在 KMS 区间 0xA0–0xBF；vendor 区从 0x40 起，`0xab-0x40` 的
    算法不适用）。它本来就返回 `ENOTTY`，不会阻塞。
  - 真正相关的还有一组 **virtio-gpu vendor ioctl**（`0x6443`、`0x6449`、
    `0x64c7`），来自 Mesa 的 `virtio_gpu` 用户态驱动；它们同样返回 ENOTTY。
  - guest 里的第一条真实错误是：`glx: failed to create dri3 screen`、
    `failed to load driver: virtio_gpu`。**内核等待超时日志一条也没有触发**
    → 卡住的是 Mesa 的 **DRI3 初始化/回退**（用户态 + 假 DRM 设备模型），
    不是 `read()`/vblank/fence 的无界内核等待。
  - 内核侧已按要求加固：事件 `read`、`WAIT_VBLANK`、atomic in-fence 等待
    全部加 1 s 超时并返回 `EAGAIN`/`ETIME`，且各带一条 warn 日志；
    10 次 glxgears 复跑不再出现 >9 分钟的挂死（每次 20 s 上限内被截断），
    但**同一个用户态 DRI3 回退问题仍在**，需要在用户态/设备模型侧修。

## A. task-exit panic

### 复现与根因

原始 panic（旧内核，来自性能基线探针）：

```
ERROR : Uncaught panic:
internal error: entered unreachable code
at /home/arch-anjie/Program/asterinas-riscv/ostd/src/task/mod.rs:210
on CPU 3 ... is_exited: true ...
```

触发场景：没有 X server 时启动 `xclock`，它立即退出。`exit_current()`
在 `reschedule()` 之后只有 `unreachable!()`；当调度决策与切换竞争把
一个**已退出**的任务再次切回来时，函数从 `reschedule()` 返回并命中
`unreachable!()`（与 issue #1633 的决策/切换非原子同族）。

### 修法

`ostd/src/task/scheduler/mod.rs`：把末尾的 `unreachable!()` 改为循环：

- 每次 `reschedule()` 返回都视为“已退出的任务又被调度回来”，
  重置 `is_first_try` 后**再次走退出流程**（任务已在 `update_current(Exit)`
  里出队，操作幂等）；
- 前 3 次打印 `exiting task was scheduled again; retrying exit`，便于现场
  确认命中；
- 不再有 panic 路径。

新增 ktest（`ostd/src/task/mod.rs`）：`concurrent_task_exit` 并发创建并
运行 64 个短命任务，等待计数到 64；覆盖退出路径的并发 churn。

### 改前/改后

| 场景 | 旧内核 923ca5c5 | 新内核（含 A） |
|---|---|---|
| 无 X 启动 `xclock`（原始触发） | 曾 panic 1 次 | 200 次退出，0 panic |
| fork/exec churn | — | 3000 次，0 panic |
| 线程退出 churn | — | 2000 次，0 panic |
| 矩阵 7 域 | — | 全 ok，panic 0 |

（原始 panic 不是 100% 可复现；这里改的是**结构性**的 panic 路径：
代码里已不存在 `exit_current` 的 `unreachable!()`。）

## B. DRM 挂死

### 先确认“谁在等”

用与挂死时完全相同的环境（Xorg 以 `GALLIUM_DRIVER=softpipe` 启动、
`glxgears` 用默认 llvmpipe），在内核每个等待路径加日志后复跑：

```
drm: unknown ioctl minor=Primary cmd=0xc0106443   (VIRTGPU vendor)
drm: unknown ioctl minor=Primary cmd=0xc01064c7
drm: unknown ioctl minor=Primary cmd=0xc01064ab   (MODE_SETPROPERTY)
drm: unknown ioctl minor=Primary cmd=0xc02064a5
drm: unknown ioctl minor=Primary cmd=0xc01064ab
once_glx rc=124 out=glx: failed to create dri3 screen|failed to load driver: virtio_gpu|
```

- **没有任何** `drm: event read timed out` / `WAIT_VBLANK timed out` /
  `atomic in-fence wait timed out` 日志 → 挂起点不在我们加固的三条
  内核等待路径上；
- 客户端自己报 `failed to create dri3 screen` / `failed to load driver:
  virtio_gpu`：Mesa 看到假 card0 声称的 `virtio_gpu`，尝试加载对应的
  用户态 DRI 驱动，失败后在 DRI3 回退路径里挂住；
- `SETPROPERTY`、vendor ioctls 都**已正确返回 ENOTTY**，没有阻塞。

### 内核侧加固（已提交，保留）

| 位置 | 原行为 | 现行为 |
|---|---|---|
| `DrmFile::read_at`（file.rs） | 无事件时无限等待 | 1 s 后返回 `EAGAIN`（+warn） |
| `drm_wait_vblank`（ioctl/kms.rs） | 无 vblank tick 时无限等待 | 1 s 后返回 `ETIME`（+warn） |
| atomic in-fence（ioctl/atomic.rs） | `fence.wait(None)` 无限等待 | 1 s 后返回 `ETIME`（+warn） |

回归（10 次、每次 20 s 上限，环境同上）：10/10 都在 20 s 内被截断
（不再有 >9 分钟的挂死），但仍全部 `rc=124`，且无内核超时日志 →
**挂死仍在用户态**。不能宣称 B 已修复。

### B 的下一步（已排除 + 下一步）

- 已排除：unknown ioctl 阻塞、事件 read 无界等待、vblank 无界等待、
  in-fence 无界等待（都加了超时并验证日志未触发）。
- 下一步（用户态/设备模型，不做 GPU 3D）：
  1. 让假 card0 不要把 Mesa 引向 `virtio_gpu` 用户态驱动（驱动名/能力上报，
     或明确只暴露 dumb-KMS + swrast）；
  2. 或者统一桌面门禁的软件 GL 环境（Xorg 与客户端都强制
     `LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe`），并给
     Firefox/glxgears 前后数字；
  3. 若仍要 DRI3 路径，需要补齐 Mesa 启动时用到的 vendor ioctls
     （`0x6443/0x6449/0xc7` 对应的 virtgpu 资源上下文），这属于设备模型
     兼容，不是 GPU 加速。

## C. 复现

```sh
# A：退出 churn（200 xclock + 3000 fork/exec + 2000 thread exit）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/exit_churn.sh make test_x86_daily_use …

# B：10 次 glxgears（Xorg softpipe、客户端 llvmpipe，即挂死环境）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/drm_hang_regress.sh make test_x86_daily_use …

# 矩阵 7 域（toolchain4 镜像）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/daily_use_checklist_regress.sh make test_x86_daily_use …
```

## D. 证据

- `evidence/2026-10-27-stability-two-defects/exit-churn.txt`（A：0 panic）
- `…/drm-once.txt`（B：unknown ioctl + `failed to create dri3 screen`，
  无内核等待超时）
- `…/drm-ten.txt`（B：10×20 s 截断）
- `…/matrix.txt`（7 域 ok，panic/alloc 0）
- 提交：`fix(ostd): make task exit idempotent instead of panicking`、
  `fix(dri): bound kms waits and return enotty/eagain instead of hanging`
