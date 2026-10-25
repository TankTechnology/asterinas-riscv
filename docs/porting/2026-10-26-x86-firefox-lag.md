# x86 Firefox 卡顿：先验证 AVX 假设，再量化 usr/sys 与 A/B/C

日期：2026-10-26（承接 2026-10-25 x86 性能任务）
内核：`923ca5c53ea46e02`　镜像：`toolchain4`（2.1 GiB，420 包）
门禁：QEMU/KVM，guest 4 vCPU / 4 GiB（SMP 对照另跑 8 vCPU）
**未做任何 GPU 驱动/3D 工作。**

## 结论摘要（先回答最关键的假设）

- **“kernel/src 没有 XCR0/OSXSAVE，所以用户态只能用 SSE2”这条线索不成立**：
  `kernel/src` 是**死树**（不参与构建）；活代码在 `ostd`，它已经设置
  `CR4.OSXSAVE` 并把 XCR0 编程为 `x87|SSE|AVX(|AVX-512)`：
  - `ostd/src/arch/x86/mod.rs:202`（`cr4 |= OSXSAVE`）
  - `ostd/src/arch/x86/mod.rs:218-226`（XCR0 = SSE / AVX / AVX-512 位）
  - `ostd/src/arch/x86/cpu/context/mod.rs:606`（上下文切换用 XSAVE/XRSTOR，
    mask `0b1110_0111`）
- guest 实测：
  - `xgetbv` → **XCR0 = 0x7**（x87|SSE|AVX），rc=0；
  - 手写 AVX2 程序执行成功（`avx2=2000000`）；
  - `/proc/cpuinfo` flags 含 `avx`、`avx2`、`fma`、`xsave`；
  - **glxinfo renderer = `llvmpipe (LLVM 19.1.7, 256 bits)`** —— 256 bits
    就是 AVX2 宽度（SSE 会是 128 bits）。
  - AVX-512 探针 SIGILL 是**宿主 i5-1340P 本身没有 AVX-512**（CPUID 决定），
    不是内核缺口。
- **因此 P1（让用户态用 AVX2）已经是完成状态，本轮不需要也不应该改
  XCR0**。瓶颈在别处（见下）。

## 1. Firefox A/B/C 测量（softpipe 强制组）

条件：Xorg 由探针启动（`GALLIUM_DRIVER=softpipe`），本地 400 行页面，
`xdotool key Page_Down` ×20；CPU ticks 来自 Firefox 所有进程的
`/proc/<pid>/stat`（`CLK_TCK=100`，1 tick=10 ms）。

| 组 | 设置 | 启动 (ms) | 滚动 20 页 (ms) | user ticks | sys ticks |
|---|---|---|---|---|---|
| A | 默认 | **619** | **639** | 2.10 s | **11.61 s** |
| B | 关 WebRender + 关 layers 加速 | 656 | 1461 | 5.48 s | 35.46 s |
| C | software WebRender + ipc=2 | 629 | 1075 | 3.78 s | 29.85 s |

- **B/C 都比 A 差**：关掉合成/加速反而更慢，说明“合成开销”不是主因；
- **sys 时间显著高于 user（约 3–5×）**：卡顿主要在**内核/显示路径**，
  不是 llvmpipe 的算术。

## 2. llvmpipe（默认 Mesa 选择）与 SMP 对照

不设 `GALLIUM_DRIVER`，让 Mesa 自己选：

| 条件 | renderer | glxgears | Firefox 启动 | 滚动 20 页 | user / sys |
|---|---|---|---|---|---|
| 4 vCPU | llvmpipe 256 bits | 330–356 FPS | 2469 ms | 807 ms | 3.47 s / 10.79 s |
| **8 vCPU** | llvmpipe 256 bits | **359–382 FPS** | **1808 ms** | 832 ms | 3.15 s / 9.90 s |

- 4→8 vCPU：glxgears **+7~8%**、Firefox 启动 **−27%**、滚动基本不变；
- 证明 llvmpipe 本身健康（300+ FPS），AVX2 生效；SMP 是廉价的小幅收益。

## 3. 显示路径与两个异常

- 本门禁的 QEMU 配置是 `virtio-gpu + 假 /dev/dri/card0`，
  **没有 `/dev/fb0`**（`ls /dev/fb*` 不存在）。因此 P2/P3 的
  “fb0 零拷贝/damage”在本配置下**无法测量**，需要用户那套 fbdev 环境
  才能对比。
- 观察到一次 `drm: unknown ioctl minor=Primary cmd=0xc01064ab`，随后
  `glxgears` 在 fake card0 上**挂起 >9 分钟**（同一探针后来的两次运行
  又正常）。这是**假 DRM 的 stub 覆盖不全 + 偶发阻塞**，属于
  CPU 侧显示兼容性问题，会让 Firefox 的 GL/合成路径偶发卡死——这比
  AVX 更像“非常卡”的候选解释。

### 3.1 另一次真 panic：任务退出路径被再次调度

首轮探针在没有 X server 的情况下启动了 `xclock`，它立刻退出；
随后内核 panic：

```
ERROR : Uncaught panic:
internal error: entered unreachable code
at ostd/src/task/mod.rs:210
on CPU 3 ... is_exited: true ...
```

`exit_current()` 末尾的 `unreachable!()` 被命中，说明一个
`is_exited: true` 的 ostd 任务被**再次调度回到退出路径**（与之前
`before_switching_to` 的决策/切换竞争同族，issue #1633）。
这不是 GPU 问题，而是**日常稳定性**的直接风险；建议下一轮：
① 把 `exit_current` 的 `unreachable!()` 改为“再次走退出流程”的安全网；
② 在调度器 `pick_next`/`enqueue` 拒绝 `is_exited`/`switched_to_cpu`
   的任务；③ 用短命进程 churn（fork/exit 1000 次 + X 客户端反复起停）
   做回归。证据：`evidence/2026-10-26-firefox-lag/panic-exit-current.txt`。

## 4. 前 3 个瓶颈判断

1. **内核/显示 syscall 路径（最大）**：Firefox 的 sys 时间是 user 的
   3–5 倍；fake card0 的 `drm_mode_*` stub 有未实现 ioctl 和偶发挂起。
   优先补 stub 并消除阻塞（不是 GPU 加速，只是让 CPU 软件渲染的
   查询/交换路径不阻塞）。
2. **显示拷贝 / Xorg 合成路径**：本门禁无 fb0，且第一次“全屏重绘
   201 ms”是在没有 X server 时测的（xsetroot 立即失败），该数字无效；
   有效的全屏重绘/拷贝数字要在 fbdev 环境下重测。届时按 P2/P3 做
   damage + 双缓冲/write-combining。
3. **guest vCPU 数（4）**：宿主 16 线程，llvmpipe/Firefox 多进程；
   SMP=8 已测出启动 −27%、glxgears +7%。把桌面门禁默认 4 → 8 是
   低风险收益。

## 5. P1–P4 状态与建议

| 优先级 | 结论 |
|---|---|
| **P1 AVX2** | **已经是完成状态**（XCR0=0x7、llvmpipe 256 bits、AVX2 可执行）。**不做改动**；若强行“补 XCR0”只会重复已有逻辑。 |
| **P2 fb 零拷贝/双缓冲** | 当前 QEMU 无 `/dev/fb0`，无法改/测；需要在 fbdev 配置（用户实测环境）上补 `FBIOPAN_DISPLAY` + WC mmap 并给前后数字。 |
| **P3 damage 区域更新** | 同上，依赖 fbdev/Xorg 驱动路径；先做 P2 再评估。 |
| **P4 Xorg 配置** | 可立即做：A/B 已证明关合成更慢，故不做“全关”；可评估 `backing store off`、`AccelMethod`、`TearFree` 等小项，并配 SMP=8。 |

## 6. 复现

```sh
# ① AVX/CPU 审计（基线探针，含 xgetbv/AVX2）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/perf_baseline.sh make test_x86_daily_use …

# ② Firefox A/B/C（softpipe，强制对照）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/firefox_lag_probe.sh make test_x86_daily_use …

# ③ llvmpipe 默认（4 vCPU 与 8 vCPU）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/firefox_llvmpipe_probe.sh make test_x86_daily_use … 
#   加 X86_DEBIAN_SMP=8 再跑一次
```

## 7. 证据

- `evidence/2026-10-26-firefox-lag/`：三组探针的串口摘录（CPU flags、
  xgetbv、AVX2、renderer、A/B/C 与 SMP 数字、DRM unknown ioctl）
- 全量串口：`target/ff-lag/console.txt`、`target/ff-llvmpipe/console.txt`、
  `target/ff-llvmpipe-smp8/console.txt`、`target/perf-baseline2/console.txt`
