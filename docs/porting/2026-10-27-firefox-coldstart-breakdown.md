# Firefox 冷启动 5.3× 的三段分解

日期：2026-10-26
内核：`e8b4a8194c0918ac`（未重编译）
探针：`/tmp/vb/coldstart2_probe.sh`（guest 内用 gcc 编译 `LD_PRELOAD` 打点库 + `/proc/syscall_time`）
门禁：`/tmp/vb/coldstart2_gate_x86.sh`、`coldstart2_gate_linux.sh`

## 0. 结论先行

冷启动里 **约 82 % 花在「首个 `XMapWindow` 之后到窗口真正可见」这一段**（Asterinas 4.69 s），
exec + 动态链接 + libxul 加起来只有约 163 ms（3 %），X 连接本身 1.6 ms。
Linux 侧结构相同（映射后也占了约 95 %），所以差别不在"哪一段"，而在**每一段的绝对成本**：

| 里程碑 | Asterinas | Linux | 倍率 |
|---|---|---|---|
| `/bin/true` 启动（exec+装载器） | 15–21 ms | 1–2 ms | ≈ 12× |
| `t1_ctor`（exec+初始链接完成） | 11.2 ms | 0.7 ms | **16×** |
| `dlopen(libxul.so)` 载入+重定位 | 106.9 ms | 5.9 ms | **18×** |
| `XOpenDisplay` 本身 | 1.6 ms | 0.4 ms | 4× |
| 到首个 `XMapWindow` | 1033.6 ms | 53.0 ms | **19.5×** |
| 到窗口可见（同轮总时间） | 5719 ms（干净轮 4466） | 1053 ms（干净轮 892） | **5.4×（5.0×）** |
| 其中「map→可见」 | 4685 ms | 1000 ms | 4.7× |

内核侧把这段时间的 CPU 成本排序：**`read` 5710 次共 3.19 s CPU（558 µs/次）** 是最大单项，
`clone` 15 次共 0.85 s（56 ms/次）次之；`mmap` 59 µs/次、`openat` 54 µs/次。

## 1. 进程内里程碑（C3，Asterinas）

打点方式：`LD_PRELOAD` 库在构造器、`dlopen`、`XOpenDisplay`、`XMapWindow` 处各打一个
`CLOCK_REALTIME` 时间戳，与 shell 的 `date +%s%N` 同钟，直接算差。

```
PROBE t0_launch_ns=1793006225669484573
PROBE t3_window_ns=1793006231388004786          # xdotool 首次命中可见窗口
PROBE segs=rel_ms t1_ctor 11.2|rel_ms dlopen_libxul_enter 56.5|rel_ms dlopen_libxul_exit 163.4|
           dur_ms dlopen_libxul 106.9|rel_ms xopen_enter 220.5|dur_ms xopen 1.6|
           rel_ms xmap_enter 1033.6|dur_ms xmap_first 0.0|n_dlopen 112
PROBE mark_window_polls=22
```

| 段 | 区间（相对启动） | 耗时 | 占本轮 5719 ms |
|---|---|---|---|
| ① exec + 动态链接（到 `t1_ctor`） | 0 → 11.2 ms | **11.2 ms** | 0.2 % |
| ② Firefox 早期初始化（到 `dlopen(libxul.so)`） | 11.2 → 56.5 ms | 45.3 ms | 0.8 % |
| ③ `dlopen(libxul.so)` 装载 + 重定位 | 56.5 → 163.4 ms | **106.9 ms** | 1.9 % |
| ④ 到 X 连接完成（`XOpenDisplay` 本身 1.6 ms） | 163.4 → 220.5 ms | 57.1 ms | 1.0 % |
| ⑤ X 连接后到首个 `XMapWindow` | 220.5 → 1033.6 ms | **813.1 ms** | 14.2 % |
| ⑥ `XMapWindow` 到窗口可见（`xdotool` 首次命中） | 1033.6 → 5719 ms | **4685.4 ms** | **81.9 %** |

按任务书要求的三段归纳：

| 段 | 内容 | 耗时（本轮） | 占比 |
|---|---|---|---|
| **①** | 进程启动 + 动态链接 + libxul | **163.4 ms** | 2.9 % |
| **②** | X 连接（含到首个 `XMapWindow`） | **870.2 ms** | 15.2 % |
| **③** | `XMapWindow` → 窗口可见 | **4685.4 ms** | **81.9 %** |

**占比最大：第 ③ 段（4.69 s，82 %）。**

## 2. 这一轮的绝对量与干净基线的差异（如实记录）

| 运行 | 总耗时 | 说明 |
|---|---|---|
| C3 打点轮（`LD_PRELOAD`） | **5719 ms** | 112 次 `dlopen` 都被 interpose，偏慢 |
| C3 干净轮（无 `LD_PRELOAD`，无 syscall 计时） | **4466 ms** | 同一次引导内 |
| P7/P8 基线（前一轮任务） | 5238 / 4944 ms | 跨引导波动 |

所以**用 5719 ms 这一轮算占比、用 4466–5238 ms 报绝对量**；若按占比折算到 4466 ms，
第 ③ 段约为 3.66 s。

## 3. 内核侧成本（C3，`/proc/syscall_time`）

采集窗口：启动 Firefox 后固定等 6 s（不轮询，避免探针自己的 `xdotool` 污染）。
对照窗口：空等 6 s（`ctrl_execve=2 2049098`、`ctrl_mmap=61 647966`、`ctrl_read=15 97765`，可忽略）。

```
PROBE sys_read=count=5710 cpu_ns=3188111623 avg_ns=558338
PROBE sys_poll=count=18652 cpu_ns=418712148 avg_ns=22448
PROBE sys_mmap=count=3740 cpu_ns=221655424 avg_ns=59266
PROBE sys_openat=count=2459 cpu_ns=133028990 avg_ns=54098
PROBE sys_top6=0 5710 … 3188111623|56 15 847224965 0 847224965 56481664|7 18652 … 418712148|202 19017 … 236441251|9 3740 … 221655424|16 724 … 211270423
```

| syscall | 次数 | CPU 合计 | 单次 CPU |
|---|---|---|---|
| `read` (0) | 5 710 | **3.19 s** | **558 µs** |
| `clone` (56) | 15 | 0.85 s | **56.5 ms** |
| `poll` (7) | 18 652 | 0.42 s | 22 µs |
| `futex` (202) | 19 017 | 0.24 s | 12 µs（另有 173 s 阻塞） |
| `mmap` (9) | 3 740 | 0.22 s | 59 µs |
| `ioctl` (16) | 724 | 0.21 s | 292 µs |
| `openat` (257) | 2 459 | 0.13 s | 54 µs |

6 s 窗口、4 vCPU 下合计约 5.1 s CPU。

## 4. 任务 B：最大段（③）的单变量定位

- 段 ③ 的内容是 Firefox 自己的启动流水线（profile、prefs、addon、内容进程拉起）到真正
  show 主窗口之间；打点显示这段时间**不在 X 侧**：`XOpenDisplay` 只花 1.6 ms，
  首个 `XMapWindow` 之后窗口内容还要等 4.7 s 才可见。
- 内核侧同期最大的单项是 **`read`：5710 次、3.19 s CPU、平均 558 µs/次**。
  这个单次成本异常（Linux 上同规模读取通常在 5–20 µs 量级），是下一步最该单独验证的点。
- 第二可疑的是 **`clone`：15 次、平均 56.5 ms**（与之前 fork 测量的固定成本 1.5–1.7 ms 不符，
  说明这些 clone 带着大 VMA/COW，落在已知的 fork 慢路径上）。
- **只定位，未改任何代码。**

## 4.1 Linux 侧同探针原始行（L4）

```
PROBE true_ms_1..5=2/1/1/1/1
PROBE lds_true=total startup time in dynamic loader: 44056 cycles (relocation 2150)
PROBE lds_ff=48636 cycles (relocation 4242) / 161628 cycles (relocation 83861)
PROBE t0_launch_ns=1793006308647642751
PROBE t3_window_ns=1793006309700269275
PROBE segs=rel_ms t1_ctor 0.7|dlopen_libxul_enter 9.9|dlopen_libxul_exit 15.8|dur_ms dlopen_libxul 5.9|
           xopen_enter 19.8|dur_ms xopen 0.4|xmap_enter 53.0|n_dlopen 191
PROBE clean_total_ms=892
PROBE clean_polls=14
```

注意两点：
- Linux 侧 `n_dlopen=191`，Asterinas 侧 112——两侧 Firefox 走的动态装载序列不完全相同，
  所以 libxul 那 18× 里可能混有路径差异，报数时按"同一里程碑"比更稳妥。
- Linux 侧 `sys_on_rc=2`（该镜像里 `asterinas` 用户没有免密 sudo），因此 **Linux 的
  syscall 成本没有取到**，本报告的 syscall 表只有 Asterinas 一侧，对照列为空。

## 5. 下一步建议（按性价比）

1. **`read()` 微基准（单变量）**：同一镜像、同一文件，两侧各读 1 万次 4 KiB，
   比 `avg_cpu_ns`；如果复现 558 µs，就在 `kernel/core` 的 read 路径上找（很可能与
   `read` 走 page-by-page copy 或每次拿锁有关）。
2. `clone` 的 56 ms：用已收敛的 fork 基准（`fork_bench_check.sh`）对照 Firefox 的
   VMA 规模，确认是 COW 还是固定成本。
3. 若 read 修复，再复测冷启动，看段 ③ 是否同步下降。

## 6. 任务 C（滚动 +103 % / 输入 +230 %）：未做

时间用在本任务 A/B 的三次门禁上（C1 因探针自身阻塞作废、C2 拿到首版数据、C3 修正测量后定稿），
任务 C 未开始。若要做，入口是现成的：在滚动/输入阶段前后读 `/proc/syscall_time`
（本报告 §3 已给出该方法与污染控制方式），重点看 `poll`/`read`/`ioctl` 的 CPU 成本。

## 7. 证据

- `evidence/2026-10-27-firefox-coldstart-breakdown/C3-probe-lines.txt`（39 行，含 segs / syscall 原始行）
- `…/C2-probe-lines.txt`（42 行，第一版探针；`LD_DEBUG` 段曾阻塞，保留以存证）
- `…/L4-probe-lines.txt`（48 行，Linux 同探针对照）
- 控制台：`~/vb-ws/C{2,3}/console.txt`、`~/vb-ws/L4/console.txt`
- 探针：`/tmp/vb/coldstart2_probe.sh`（含 guest 内 `mark.c` 源码）
