# Firefox sys 瓶颈：A（Mesa 驱动广告）与 B（syscall 归因）的实测结果

日期：2026-10-26/27
内核：A 实验 `e619f3a9`（driver name 改为 simpledrm；**已回退**）、
B 测量 `f3299cd5`（A+B 稳定性修复 + 等待日志）
镜像：`ff-lag`（toolchain4 + firefox-esr + x11-apps）

## 结论摘要

- **A 的假设被证伪**：只把 `DRM_IOCTL_VERSION` 的 driver name 从
  `virtio_gpu` 改成 `simpledrm` 并没有修好挂死，客户端报错从
  `failed to load driver: virtio_gpu` 变成
  `failed to load driver: simpledrm`，仍然挂。改动已回退。
  - 真正可控的因素是 **GL 环境一致性**：Xorg 与客户端都用默认
    llvmpipe 时 glxgears 正常（330–382 FPS，20 s 内退出）；Xorg 用
    softpipe、客户端用 llvmpipe 的**混搭环境**才挂。
  - 记录为 **workaround**：桌面/GL 客户端统一
    `LIBGL_ALWAYS_SOFTWARE=1 GALLIUM_DRIVER=llvmpipe`（两边一致），
    这是环境修正，不是内核修复。
- **B 的 top-10 syscall 表在本内核上取不到**：
  - `PTRACE_ATTACH` 返回 `Input/output error` → 无法 attach 运行中的
    Firefox；
  - 从 exec 起 strace 只记录到启动器（93 个 syscall，`clone3` 1 次
    error），Firefox 在 strace 下到不了窗口，拿不到滚动期间的直方图；
  - `/proc/<pid>/stat` 的 `minflt/majflt` 在本内核恒为 0 → 缺页归因不可用。
- 仍可确认的事实：固定 pid 集合的 `utime/stime` 累计为
  **user 10.38 s / sys 70.92 s（约 1:6.8）**，与之前 A/B/C 组的
  “sys 主导”一致；但无法进一步拆到具体 syscall。

## A. 掐掉 Mesa 的 virtio_gpu 广告（负结果）

| 项 | 值 |
|---|---|
| 改动 | `kernel/comps/drm-virtio-gpu/src/lib.rs` 的 `DRIVER_NAME`：`virtio_gpu` → `simpledrm`（内核 e619f3a9） |
| 环境 | Xorg 以 `GALLIUM_DRIVER=softpipe` 启动，`glxinfo/glxgears` 用默认 llvmpipe（即第一次挂死的环境） |
| glxinfo | 20 s 超时截断（rc=124），`glx: failed to create dri3 screen` / `failed to load driver: simpledrm` |
| glxgears | 同样 20 s 截断（rc=124），同上错误 |
| 结论 | **改名不解决问题**；Mesa 无论如何都会按名字加载一个 DRI 驱动，失败后卡在 DRI3 回退。改动已回退，未提交。 |

对照（同一镜像、内核，Xorg 与客户端都不设置 GALLIUM_DRIVER）：
`glxinfo` = llvmpipe (LLVM 19.1.7, 256 bits)，`glxgears` = 330–382 FPS，
**不再挂**。因此可操作修法是环境统一，而不是驱动名。

## B. sys 时间归因（方法与阻塞）

| 方法 | 结果 | 第一条真实错误 |
|---|---|---|
| `strace -c -f -p <pid>` attach 运行中 Firefox | **不可用** | `strace: attach: ptrace(PTRACE_ATTACH, 134): Input/output error` |
| 从 exec 起 `strace -c -f firefox-esr` | 仅启动器 93 个 syscall；Firefox 未到窗口 | trace 中 `clone3` error=1；无渲染期样本 |
| `/proc/<pid>/stat` minflt/majflt | 恒为 0 | 该内核未导出这两项 |
| `/proc/<pid>/stat` utime/stime（固定 pid 集） | **user 1038 ticks / sys 7092 ticks ≈ 1:6.8** | — |

固定 pid 集那次只匹配到 Firefox 主进程（comm `firefox`；内容进程的
comm 形如 `Isolated Web Co`，未纳入），`minflt=majflt=0`。

**top-10 syscall 表：无法给出**（不是没测，是当前内核缺少 ptrace attach
与缺页计数这两条观测通道）。要得到它，必须先做下面任一“观测能力”修复：

1. 实现/修复 `PTRACE_ATTACH`（至少 attach + 读 syscall 入口/返回），这是
   strace 的硬前提；
2. 或在 procfs 暴露每任务的 syscall 计数（如在 syscall dispatch 里累计
   `(nr, count, ns)`，经 `/proc/<pid>/syscall_counts` 读出）；
3. 同时把 `minflt/majflt` 按 Linux 语义填进 `/proc/<pid>/stat`。

## C. 优化（本轮未做）

没有 top syscall 数据就不改代码（纪律：没数字的优化不算完成）。
基于现有证据的候选排序仍为：

1. **futex/唤醒**（之前 flock 争用已证明唤醒路径是热点）；
2. **缺页**（llvmpipe tile buffer / Firefox JIT 代码页）；
3. **ioctl 批量**（每帧 DRM 查询）。

下一步应先补 B 的观测能力，再按数据修 top1–2。

## D. 回归

- 7 域矩阵在 A+B 稳定性内核 `f3299cd5` 上：APT/DESKTOP/APP/CLIPBOARD/
  AUDIO/USB/SUSPEND 全 ok；INPUT_METHOD（额外域）仍 fail；
  panic 0、allocation failed 0（`evidence/2026-10-27-stability-two-defects/matrix.txt`）。
- 本次 A/B 的探针均 panic 0。

## E. 证据

- `evidence/2026-10-27-firefox-sys/firefox-sysattr.txt`（三次尝试的原始输出）
- `…/matrix-on-fix-kernel.txt`（7 域回归）
- 脚本：`tools/x86/debian/{drm_advert_probe,firefox_sys_attrib,
  firefox_sys_attrib2,firefox_sys_fixedpid}.sh`
