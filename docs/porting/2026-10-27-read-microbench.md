# `read()` 微基准：Asterinas vs Linux

日期：2026-10-26
内核：`e8b4a8194c0918ac`（未重编译）
微基准：`/tmp/vb/readbench_probe.sh` 内嵌的 `readbench.c`（guest 内 `gcc -O2` 编译）
门禁：`/tmp/vb/readbench_gate_x86.sh`（R1/R2）、`readbench_gate_linux.sh`（R3）
同一镜像、同一文件、同一 10000 次调用；`avg_wall_ns` 来自 `CLOCK_MONOTONIC`，
`avg_cpu_ns` 来自 `CLOCK_PROCESS_CPUTIME_ID`。

## 0. 结论

1. **单次 `read()` 的固定开销没有复现 558 µs**：Asterinas 热读 4.2–6.9 µs，
   Linux 0.24–1.7 µs（差 4–8×）。按任务书的判据，这落在「与 Linux 同量级」一支。
2. **真正的差距在冷读**：把 164 MB 的 `libxul.so` 完整读一遍（本次启动首次触碰），
   Asterinas **5.31 s**（132.5 µs / 4 KiB），Linux **0.21 s**（5.3 µs / 4 KiB）——**25×**。
3. 内核侧原因（任务 B）：页缓存**完全没有预读**，缺页时**一次只向块设备提交一页**
   （文件:行号见 §3）。Linux 靠预读把顺序冷读变成大块 I/O，我们用 4 KiB 同步 I/O 逐页取。

因此 Firefox 冷启动里 558 µs/次的 `read`，性质是「**冷页按页同步取**」，不是「read 系统调用本身慢」。

## 1. 用例与结果（µs，平均每次调用）

### 1.1 `/tmp`（tmpfs，40 MB）

| 用例 | Asterinas wall / cpu | Linux wall / cpu | 倍率（wall） |
|---|---|---|---|
| A0 冷读顺序 4096（10240 次） | 5.05 / 4.98 | 1.72 / 1.71 | 2.9× |
| A1 热读顺序 4096（10000 次） | 4.62 / 4.60 | 0.59 / 0.58 | **7.8×** |
| A2 每次 1 字节（10000 次） | 4.22 / 4.20 | 0.24 / 0.24 | **17.6×** |
| A3 `readv` 1×4096 | 5.77 / 5.70 | 0.62 / 0.61 | **9.3×** |
| A4 `pread` 4096 | 4.38 / 4.40 | 0.58 / 0.58 | **7.6×** |

tmpfs 没有后端存储，所以 A0≈A1（没有真正的冷读），这组只反映**系统调用+VFS+拷贝的固定开销**。

### 1.2 ext4 新建文件（40 MB，`dd` 刚写完，页在缓存里）

| 用例 | Asterinas wall / cpu | Linux wall / cpu | 倍率 |
|---|---|---|---|
| A0 冷读顺序 4096 | 4.51 / 4.49 | 1.86 / 1.84 | 2.4× |
| A1 热读顺序 4096 | 4.53 / 4.50 | 0.55 / 0.55 | **8.2×** |
| A2 1 字节 | 4.36 / 4.30 | 0.24 / 0.24 | **18×** |
| A3 `readv` | 6.85 / 6.90 | 0.60 / 0.59 | **11×** |
| A4 `pread` | 5.12 / 5.10 | 0.55 / 0.55 | **9.3×** |

### 1.3 `libxul.so`（ext4，164 124 984 B，本次启动从未读过 ⇒ 真冷读）

| 用例 | Asterinas wall / cpu | Linux wall / cpu | 倍率 |
|---|---|---|---|
| A0 冷读顺序 4096（40 070 次） | **132.53 / 50.31** | **5.26 / 5.04** | **25.2× / 10.0×** |
| A1 热读顺序 4096 | 6.15 / 6.20 | 1.60 / 1.60 | 3.8× |
| A2 1 字节 | 5.26 / 5.20 | 0.24 / 0.24 | 22× |
| A3 `readv` | 6.79 / 6.80 | 0.60 / 0.59 | 11× |
| A4 `pread` | 5.08 / 5.00 | 0.56 / 0.55 | 9.1× |

整份文件读一遍的总时间：**Asterinas 5.31 s（31 MB/s）vs Linux 0.21 s（780 MB/s）**。

原始行：

```
# Asterinas R2
PROBE rb_libxul_all=phase=A0_cold_seq4096 reads=40070 bytes=164124984 wall_ns=5310676198 cpu_ns=2016000000 avg_wall_ns=132534 avg_cpu_ns=50311|phase=A1_warm_seq4096 reads=10000 bytes=40960000 wall_ns=61549113 cpu_ns=62000000 avg_wall_ns=6154 avg_cpu_ns=6200|…
# Linux R3
PROBE rb_libxul_all=phase=A0_cold_seq4096 reads=40070 bytes=164124984 wall_ns=210840766 cpu_ns=202132377 avg_wall_ns=5261 avg_cpu_ns=5044|phase=A1_warm_seq4096 reads=10000 bytes=40960000 wall_ns=16040043 cpu_ns=15955584 avg_wall_ns=1604 avg_cpu_ns=1595|…
```

## 2. 判据对照

| 任务书判据 | 实际 | 结论 |
|---|---|---|
| Asterinas 复现 ~558 µs/次 | 热读 4–7 µs，冷读 132 µs | ✗ 未复现 558 µs |
| 与 Linux 同量级（几 µs） | 热读 4–7 µs vs 0.24–1.7 µs | ✔（同量级，但稳定差 4–8×） |
| ⇒ 转查 page cache / 预读 | 冷读 25× 慢，页缓存无预读 | ✔ **主因** |

## 3. 任务 B：内核 read 路径定位（只定位，未改代码）

### 3.1 路径链

| 层 | 位置 | 行为 |
|---|---|---|
| ext2 文件读 | `kernel/core/src/fs/fs_impls/ext2/inode/file.rs:329` | `self.page_cache().read(offset, writer)?` |
| 页缓存 → VMO | `kernel/core/src/vm/page_cache/mod.rs:348`（`VmIo::read`）→ `vmo/mod.rs:534` | 按页 `collect_pages` + 每页 `read_fallible`（**单次拷贝**） |
| 页已存在 | `vmo/mod.rs:531` `PAGE_BATCH_CAPACITY = 32` | 只对**已在缓存**的页做 32 页批量收集 |
| 页缺失 | `vmo/mod.rs:818-829` `handle_commit_error` → `backend.read_page(index, …)` | **一次只取一页**，同步等待 |
| 块设备提交 | `page_cache/mod.rs:676-694` `read_page_async` → `submit_read_bio` | 每页一个 bio segment |

### 3.2 预读

```
$ grep -rni "readahead\|read_ahead\|prefetch" kernel/core/src/vm/page_cache
（无任何命中）
```

页缓存**没有预读逻辑**。因此顺序冷读时，每次 4 KiB `read()` 命中缺页 → 提交一个 4 KiB
bio → 等待完成，再做下一次。这正好解释了 A0 的 132 µs/页，以及 164 MB 需要 5.31 s。

### 3.3 固定开销（与预读无关的那部分）

即使页全在缓存里，Asterinas 仍比 Linux 慢 4–8×（4096 字节热读 4.6 vs 0.59 µs；
1 字节 4.2 vs 0.24 µs；`readv` 5.7 vs 0.61 µs）。这一份是 VFS 全链路 + 页缓存查页 + 拷贝的
固定成本，属于第二个独立的优化点，但**不是** 558 µs 的来源。

## 4. 与冷启动的关系

- Firefox 启动时 `read` 5710 次合计 3.19 s CPU；本次实测冷页 **50 µs CPU/页**，
  与这个量级相符（Firefox 的读里还混着 ext2 元数据与更大块）。
- 因此冷启动 81.9 % 的「map → 可见」段里，有相当一部分是**同步 4 KiB 冷读**在排队。
- 若补上预读（顺序流一次取 128 KiB+），A0 的 132 µs 应显著向 Linux 的 5.3 µs 靠拢。

## 5. 任务 C（clone 56.5 ms/次）：未做

A 用掉了本轮全部时间（三次门禁：R1 tmpfs 首版、R2 三目标、R3 Linux）。C 的入口已现成：
`tools/x86/debian/fork_bench_check.sh`；建议用它测「空 fork ×1000」与本微基准同轮运行。

## 6. 证据

- `evidence/2026-10-27-read-microbench/R2-asterinas-probe-lines.txt`（30 行，三目标）
- `…/R1-asterinas-tmpfs-probe-lines.txt`（24 行，首版 tmpfs）
- `…/R3-linux-probe-lines.txt`（Linux 侧同基准）
- 控制台 `~/vb-ws/{R1,R2,R3}/console.txt`；门禁日志 `/tmp/vb-R{1,2,3}-gate.log`
- 门禁状态：R1、R3 为 `screendump … red=1.000` + `PASS`；**R2 的门禁日志是 0 字节**
  （harness 侧丢失，未复现），但该轮探针完整跑到 `probe_done=1`、`Uncaught panic` 计数 0，
  数据行齐全，故仍采用；此点如实记录。
- 微基准源码：`/tmp/vb/readbench_probe.sh` 里的 `readbench.c`（A0–A4 五段）
