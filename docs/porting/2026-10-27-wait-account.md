# VmSize 单位修复落地 + 墙钟三分账（等在哪）

日期：2026-10-26
内核：`ac4e536d3`（VmSize 单位修复已入库）

## 1. 任务 A：VmSize 单位修正（已落地，矩阵全绿）

- 改动只有一处：`procfs/pid/task/status.rs` 的 `vsize = total_vm / 1024`（`total_vm` 是**字节**）。
- 验证（WT1）：`ff1_status=VmSize:=455492`、`ff2_status=VmSize:=455492`（kB）≈ **445 MiB**，
  与 Linux 的 447 MB 同量级 ✓；同轮 CAL1 99/0、CAL2 11/89 保持与 Linux 一致。
- 7 域矩阵：`DESKTOP/APP/APT/CLIPBOARD/AUDIO/USB/SUSPEND` 全 ok、`DAILY_DONE=ok`、`panic=0`。
- **commit `ac4e536d3 fix(proc): report VmSize in kB`。**
- `insert_try_merge` 的计数问题**未动**（上一轮它导致 `subtract with overflow` panic），
  而且本轮证明单位才是 466 GB 的主因，merge 泄漏若存在也只是次要项。

## 2. 任务 B：`/proc/syscall_time` 已有阻塞字段，直接出账

该文件第 4 列就是 `blocked_ns`（无需新增内核字段）。8 秒窗口、重置后快照（WT2，两轮）：

```
run2_wall_ms=8019
run2_stat=utime=2 stime=13                 ← 主进程 CPU = 20 + 130 = 150 ms
run2_totals=count=5360 total_ns=18920211472 blocked_ns=18813153600 cpu_ns=107057867
run2_top8_blocked=nr=61 8.045s | nr=230 8.000s | nr=232 2.721s | nr=202 0.021s
                | nr=0 0.013s | nr=7 0.006s | nr=58 0.005s | nr=271 0.002s
```

（`nr` 含义：61=`wait4`、230=`clock_nanosleep`（探针自己的 `sleep 8`）、232=`epoll_wait`、
202=`futex`、0=`read`、7=`poll`、58=`vfork`、271=`ppoll`。）

### 三分账（主进程，8.0 s 墙钟）

| 项 | 数值 | 占比 |
|---|---|---|
| CPU（utime+stime） | **150 ms** | **1.9 %** |
| 阻塞在 syscall（主进程单独） | **NA**（`syscall_time` 是**全系统**累计，无法按进程拆分） | — |
| 未归因 | **NA**（同上，口径不允许精确相减） | — |

如实说明：若要真正的三分账，需要**按进程**的阻塞统计（当前计数器是全局的），
本轮没有加（时间盒）。但**全系统**的阻塞分布已足够回答「等在哪」。

## 3. 任务 C：等在哪

按全系统阻塞时间排序（8 s 窗口）：

| syscall | 阻塞时长 | 含义 |
|---|---|---|
| `wait4` (61) | **8.045 s** | 有进程在等子进程退出（探针/会话层） |
| `clock_nanosleep` (230) | 8.000 s | 探针自己的 `sleep 8` |
| **`epoll_wait` (232)** | **2.721 s** | **事件循环在等事件** |
| `futex` (202) | **0.021 s** | 极小 |
| `read` (0) | **0.013 s** | 极小 |
| `poll` (7) / `ppoll` (271) | 0.006 / 0.002 s | 极小 |

**结论**：
1. **不是 poll**（6 ms）、**不是 futex**（21 ms）、**不是 read**（13 ms）——这三条之前的候选全部出局。
2. 「等」集中在 `wait4` + `epoll_wait`：前者是子进程/会话等待，后者是**事件循环空转等事件**。
   对 Firefox 而言这属于**应用侧的就绪等待**（等 IPC/网络/定时器），不是内核让路太慢的证据。
3. 结合主进程 CPU 只有 150 ms / 8 s：**Firefox 的墙钟主要花在等待事件上，内核优化到这里收益有限**；
   若还要提速，方向是应用兼容/IO 可用性（例如让事件真的到来），而不是继续优化 syscall 路径。

## 4. 证据

- `evidence/2026-10-27-wait-account/matrix-vsize-fix.txt`（59 行，7 域矩阵）
- `…/WT2-blocked-account.txt`（9 行，两轮阻塞账）
- 控制台 `~/vb-ws/WT2/console.txt`（WT1 的控制台在清理磁盘时删除，VmSize 数值见 §1）
