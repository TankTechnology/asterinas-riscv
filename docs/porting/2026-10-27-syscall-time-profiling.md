# per-syscall 计时：/proc/syscall_time 与 Firefox 归因

日期：2026-10-27
内核：`49cc2798aef0f820`　镜像：`toolchain4`（矩阵）、`ff-lag`（Firefox）

## 1. 实现

- `kernel/core/src/syscall/mod.rs`：
  - 全局 `SYSCALL_TIME_CYCLES: [AtomicU64; 512]` 与
    `SYSCALL_TIME_COUNTS: [AtomicU64; 512]`；
  - `handle_syscall` 在进入时（仅当开关打开）读一次 `rdtsc`，返回时再读
    一次，把**周期数**累加到对应 syscall 槽位；
  - 开关 `SYSCALL_TIME_ENABLED` 默认 **off**（正常路径零 rdtsc 开销）。
- `kernel/core/src/fs/fs_impls/procfs/syscall_time.rs` + 注册：
  `/proc/syscall_time`
  - 读：输出 `# enabled=<0|1>` 后按 `total_ns` 降序、
    `count>0` 的行：`<nr> <count> <total_ns> <avg_ns>`
    （周期用 `tsc_freq()` 在读取时换算成 ns）；
  - 写：`on` / `off` / `reset`。
- `tools/x86/debian/syscall_time_report.py`：读该文件，映射 x86_64 名字，
  打印 top-N 表（nr/count/total_ms/avg_us/占比）并可写 JSON。

**精度说明**：每条 syscall 计时是 `rdtsc` 两次（进入/返回）的**墙钟**
差值，包含该 syscall 内的阻塞时间——这既是它的用途（看“时间花在哪个
syscall 上”），也是它的局限（阻塞型 syscall 会被放大，见 §3）。

## 2. 验证

### 2.1 正确性 + 开销（自包含 C 基准，无 shell 等待污染）

`syscall_time_getpid.sh` 在同一个 C 程序里写 `off`→跑 1e6 次 getpid→
写 `on`/`reset`→再跑 1e6 次→读 `/proc/syscall_time`：

```
stg_getpid count=1000000 share=99.9 total_ns=247097828
off_cycles=2269562846 on_cycles=2250569891 overhead_pct=-0.84
```

- **count = 1,000,000（精确）**，getpid 占总时间 **99.9%**（>90% ✓）；
- 开/关计时的循环周期差 **−0.84%**（噪声内，<5% ✓）。

### 2.2 对照：shell 驱动的同一基准

`syscall_time_check.sh` 用 shell 连跑 3 次 getpid 循环并 `cat` 计数器：

```
st_off_us=996559 st_on_us=1001191 st_overhead_pct=0.46
st_getpid_count=3000000 share=10.3
```

- count 是 3×1e6（脚本跑了三次）✓，但占比只有 **10.3%**：同一窗口里
  两次 `wait4` 与一次 `cat` 的 `read` 墙钟分别约 3.0 s / 3.1 s，
  远超 1e6 次 getpid 的实际 CPU。这说明计数器测的是**墙钟**：
  阻塞型 syscall 会把等待时间记到自己头上。

## 3. Firefox 滚动 top10（20 s，内核 49cc2798）

启停方式：`echo on > /proc/syscall_time; echo reset > …`，启动 Firefox
并等待窗口、激活后开始滚动 20 s，再读计数器（`syscall_time_report.py`）：

| # | syscall | nr | count | total_ms | avg_us | 占比 |
|---|---|---|---|---|---|---|
| 1 | futex | 202 | 17,581 | 871,106.5 | 49,548 | **69.8%** |
| 2 | epoll_wait | 232 | 8,573 | 128,278.7 | 14,963 | 10.3% |
| 3 | poll | 7 | 42,101 | 93,431.7 | 2,219 | 7.5% |
| 4 | wait4 | 61 | 135 | 39,328.0 | 291,319 | 3.2% |
| 5 | clock_nanosleep | 230 | 96 | 36,283.8 | 377,956 | 2.9% |
| 6 | ppoll | 271 | 34 | 33,001.2 | 970,624 | 2.6% |
| 7 | recvmsg | 47 | 22,168 | 18,936.1 | 854 | 1.5% |
| 8 | sendmsg | 46 | 3,591 | 14,191.9 | 3,952 | 1.1% |
| 9 | read | 0 | 7,044 | 4,160.6 | 591 | 0.3% |
| 10 | clone | 56 | 11 | 1,787.0 | 162,450 | 0.1% |

**解读（重要）**：前 6 名里有 5 个是**阻塞型**（futex/epoll_wait/poll/
wait4/clock_nanosleep/ppoll），平均每次几十到近千毫秒——这些是等待时间，
不是 CPU 时间。真正耗 CPU 的候选是 `recvmsg`/`sendmsg`（合计 2.6% 墙钟，
但调用次数 2.5 万次，单次 0.85–4 ms）以及 `poll` 的轮询部分。
这说明：

1. 计时器工作正常、开销低，能给出“时间花在哪个 syscall 上”；
2. 但 **wall-time ≠ CPU time**，阻塞 syscall 会压倒统计；
3. 要得到 CPU-only 归属，需要在 syscall 内**扣除阻塞时间**
   （例如在线程 park/unpark 时累计 blocked cycles 并从总时间中减去），
   或改成 on-CPU 采样；这是下一步。

## 4. 回归

7 域矩阵（kernel 49cc2798）：APT/DESKTOP/APP/CLIPBOARD/AUDIO/USB/SUSPEND
全 ok；INPUT_METHOD（额外域）仍 fail；panic 0、allocation failed 0。

## 5. 复现

```sh
# 自包含正确性/开销
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/syscall_time_getpid.sh make test_x86_daily_use …

# Firefox 归因（含 shell 对照）
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/syscall_time_check.sh make test_x86_daily_use …
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/ff-lag/daily.ext2

# 报告
python3 tools/x86/debian/syscall_time_report.py /proc/syscall_time --top 10
```

## 6. 证据

- `evidence/2026-10-27-syscall-time/getpid-check.txt`（1e6 / 99.9% / −0.84%）
- `…/shell-check.txt`（0.46% 开销；10.3% 的墙钟污染示例）
- `…/firefox-top10.txt`（Firefox 表）
- `…/matrix.txt`（7 域回归）
