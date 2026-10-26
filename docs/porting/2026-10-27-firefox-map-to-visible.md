# Firefox 「XMapWindow → 窗口可见」拆解（M1/M3）

日期：2026-10-26
内核：`f8091faef`（含预读）
探针：`/tmp/vb/mapwindow_probe.sh`（M1：`LD_PRELOAD` 加 Expose/FcInit/glxtest 钩子 + 4 组开关）、
`/tmp/vb/mapwindow2_probe.sh`（M3：无 preload，采样 CPU/进程/syscall）

## 0. 结论与可信度

1. **「map → 可见」这一段确实是冷启动的大头**：两次独立测量分别是 **67 %**（M1）与
   **≥ 71 %**（M3，干净轮：可见窗口在 7.29 s，其中 map 之前只有约 1 s 级）。
   这与 `6fd578e32` 的 81.9 % 一致。
2. **段内拆分（map→expose→paint→ready）本轮没拿到可信数字**，必须如实说明：
   M1 用 `XNextEvent` 钩子记录的「首个 Expose」是 7.46 s（map 之后 5.15 s），而 M3 干净轮里
   窗口标题在 map 之后仅 **1.45 s** 就已变成页面标题。两者矛盾，说明 GTK 的暴露事件不是
   经 `XNextEvent` 送达（GDK 还可能走 `XCheckIfEvent`/`XWindowEvent`），该钩子测到的是
   「Firefox 主循环第一次调用 `XNextEvent`」而不是「首个 Expose 到达」。
   **所以本报告不把 map→expose 当作有效拆分**，只保留「map 之后这一段最大」这个结论。
3. **四组开关全部无效**（基准 4.76 s；最好的一组 4.73 s，−0.6 %，远未达 30 % 判据）：
   沙箱、profile 远端、字体缓存、WebRender 都不是主因。
4. 干净轮里 map 之后 Firefox 进程树持续消耗 CPU（采样见 §3），**不是空闲等待**；
   下一步要查的是它到底在算什么，而不是继续找哪个开关能关掉。

## 1. M1：带进程内标记的一轮（原始行）

```
PROBE a_visible_ms=7633   a_first_paint_ms=8178   a_ready_ms=8899
PROBE a_marks=mark t1_ctor 20.4 ms|mark dlopen_enter 259.3 ms /usr/lib/firefox-esr/libxul.so|
  mark dlopen_exit 443.7 ms /usr/lib/firefox-esr/libxul.so|mark xopen_enter 558.0 ms|
  mark xopen_exit 559.8 ms|mark xmap_enter 2310.7 ms|mark xmap_exit 2310.7 ms|
  mark fcinit_enter 2342.0 ms|mark fcinit_exit 2458.6 ms|mark expose_first 7458.9 ms|
```

| 段 | 相对启动 | 长度 |
|---|---|---|
| exec + 链接 | 0 → 20.4 ms | 20.4 ms |
| libxul 载入 | 259.3 → 443.7 ms | 184.4 ms |
| X 连接 | 558.0 → 559.8 ms | 1.8 ms |
| 到首个 `XMapWindow` | 559.8 → 2310.7 ms | 1 750.9 ms |
| `FcInit` | 2342.0 → 2458.6 ms | **116.6 ms** |
| map → 首个 `XNextEvent` 里的 Expose | 2310.7 → 7458.9 ms | 5 148.2 ms（**见 §0.2 的可信度说明**） |
| → 首次像素变化 | → 8178 ms | 719.1 ms |
| → 窗口标题 = 页面标题 | → 8899 ms | 721.0 ms |

`glxtest` 的 `execve` 钩子没有触发（Firefox 可能用 `posix_spawn`/`clone`+`execve` 的其它路径），
记作「未观测到」。

这一轮总时长 8.9 s，明显高于干净基线 4.5 s：`LD_PRELOAD` 拦截了 112+ 次 `dlopen` 与所有
`XNextEvent`，加上每 50 ms 一次 `xwd` 全屏截图，属于**测量放大**。绝对值不可与基线直接比。

## 2. M1-B：开关对照（每开关 3 次冷启动，判据 ≥30 %）

| 配置 | 三次（ms） | 中位 | 相对基准 |
|---|---|---|---|
| 基准 | 4691 / 4757 / 4917 | **4757** | — |
| `MOZ_DISABLE_CONTENT_SANDBOX=1` | 4980 / 4420 / 4854 | 4854 | +2.0 % |
| `MOZ_NO_REMOTE=1`（空 profile） | 5034 / 4794 / 4809 | 4809 | +1.1 % |
| `fc-cache -f` 预热（本身 3735 ms） | 5039 / 4685 / 4803 | 4803 | +1.0 % |
| `MOZ_WEBRENDER=0` | 4730 / 5107 / 4628 | 4730 | −0.6 % |

**没有任何一组压缩 ≥30 %**，最接近的 `MOZ_WEBRENDER=0` 也只有 −0.6 %（在噪声内）。
⇒ 沙箱初始化、profile 远端/锁、字体扫描、WebRender 都不是主因。

## 3. M3：干净轮的时间线（CPU/进程/标题）

```
PROBE map_ms=7292           # findwin 首次命中
PROBE s1 delta_ticks=828 procs=1 ready=0
PROBE s2 delta_ticks=804 procs=1 ready=1     # 标题已是 READYTEST
...
PROBE s7 delta_ticks=1574 procs=1 ready=1
PROBE ready_ms=8746
```

- map 之后 **1.45 s** 文档就已就绪（`READYTEST`），且进程树每个采样窗口都在消耗数百个
  CPU tick ⇒ 这段时间是**计算**，不是等 I/O 或等锁。
- CPU tick 的绝对换算不可靠（`getconf CLK_TCK=100`，但采样循环本身要 fork `pgrep`/`awk`/`xdotool`，
  每个"0.5 s"实际约 1 s 以上，且会把探针自己的 CPU 计进去），所以只取**趋势**：
  map 之后持续忙，`ready=1` 之后 tick 迅速降到 356→130→27→18（收尾）。

## 4. syscall 侧（M3，**已被采样循环污染，仅作参考**）

```
PROBE sys_read=count=24117 cpu_ns=6967741124 avg=288914
PROBE sys_clone=count=517  cpu_ns=3868750114 avg=7483075
PROBE sys_openat=count=20785 cpu_ns=1268858775
PROBE sys_poll=count=54884 cpu_ns=1189780878
PROBE sys_execve=count=803 cpu_ns=1133342582
```

`execve` 803 次、`openat` 20785 次、`read` 24k 次里含有探针每轮 fork 的 `pgrep`/`awk`/`xdotool`
（每 0.5 s 一轮、16 轮），**不能当作 Firefox 的账**。任务 C 要求的「top-5 syscall 对照」因此
**未取得可信数据**，下一轮应在无轮询的固定窗口内采集（方法已在 `6fd578e32` §3 给出）。

## 5. 下一步（按可信度排序）

1. **换一个可靠的 expose 信号**：钩 `XCheckIfEvent`/`XWindowEvent`/`XGetEventData`，或者
   直接钩 GDK 的 `gdk_window_show`/`gdk_frame_clock` 提交回调；先用 M3 的「标题就绪」做交叉校验
   （本次它把 map 之后压到 1.45 s，说明剩余的大头不在 X 事件派发，而在 Firefox 自己的初始化）。
2. **对 map 之后的 Firefox 进程树做采样剖析**（同进程树、固定 5 s 窗口、不 fork 轮询：
   读 `/proc/<pid>/stat` 或用已实现的 `/proc/syscall_time`，把探针自身开销排除）。
3. 关注 `clone`（M3：517 次、7.5 ms/次）、`read`（289 µs/次，即便有了预读）这两项——
   它们仍是 Firefox 启动里最贵的两个系统调用。

## 6. 证据

- `evidence/2026-10-27-firefox-map-to-visible/M1-probe-lines.txt`（36 行：标记 + 开关矩阵）
- `…/M3-probe-lines.txt`（36 行：CPU/进程/标题采样 + syscall）
- 控制台 `~/vb-ws/M1/console.txt`、`~/vb-ws/M3/console.txt`
- 探针：`/tmp/vb/mapwindow_probe.sh`、`/tmp/vb/mapwindow2_probe.sh`
- 对照基线：`evidence/2026-10-27-page-cache-readahead/P9-firefox-probe-lines.txt`
