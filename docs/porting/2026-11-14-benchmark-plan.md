# RISC-V 基准测试：调研结论与实施计划

日期：2026-11-14
目标平台：Megrez（EIC7700，4×RV64 @1.6 GHz，16 GiB，SD/eMMC 存储，千兆有线）
对照平台：同一块板子上的 Linux（RockOS 厂商内核 6.6）与同配置 QEMU

## 1. 已有的基础设施（不要重造）

`tools/riscv/perf/` 已经有一套**可复现测量契约**，本计划接在它上面：

| 组件 | 作用 |
| --- | --- |
| `collect_samples.py` | 丢弃 warmup，采 5 个样本，保留 stdout/stderr/退出码/耗时，要求 `PERF_SAMPLE=<number>` 单行 |
| `aggregate_results.py` | 输出 median / p95 / 标准差，以及 `ratio_asterinas_over_linux` |
| `audit_baseline.py` | 发布前审计：LMBench 覆盖 + ext4 基线是否为 main 分支 |
| `build_lmbench_subset.sh` / `build_fio.sh` | 交叉编译 riscv64 版 LMBench 子集与 fio |
| `bench_gate.py` / `fio_gate.py` | 已有的 CPU/IO 门禁 |

约定：**同一 QEMU 机型、同一 CPU 型号、同一内存/SMP、同一 virtio 拓扑、同一份
Debian 用户态**；Asterinas 用 `RELEASE=1`；记录 Linux 内核版本与 mitigation 状态。

## 2. 调研结论：候选套件与取舍

| 套件 | 覆盖 | riscv64 交叉编译 | 板端可行性 | 结论 |
| --- | --- | --- | --- | --- |
| LMBench 子集（已有） | syscall / 进程 / 文件 / 网络延迟 | 已有脚本 | 已跑通 | **采用**（延迟主指标） |
| fio（已有） | 顺序/随机读写、IOPS、延迟分布 | 已有脚本 | 已跑通 | **采用**（存储主指标） |
| iperf3 | TCP/UDP 吞吐、连接时延 | 纯 C，`--host=riscv64-linux-gnu` 即可 | 需对端 | **采用**（网络） |
| hackbench / schbench | 调度器、唤醒延迟、SMP 扩展性 | 纯 C | 可 | **采用**（并发） |
| sysbench | CPU、内存、线程、互斥、文件 IO | autotools，交叉编译可行 | 可 | **采用**（补充 CPU/内存/线程） |
| STREAM / mbw | 内存带宽 | 纯 C | 可 | **采用**（内存带宽，短小） |
| fs-mark | 元数据密集型文件操作 | 纯 C，Makefile 简单 | 可 | **采用**（元数据，弥补 fio 只测数据面） |
| filebench | 预定义负载（varmail/webmail/fileserver） | 需 autoconf + 依赖，交叉编译繁琐 | 依赖多 | **暂缓**（fs-mark 覆盖元数据面） |
| Postmark | 小文件并发混合负载 | 纯 C | 可 | **可选**（与 fs-mark 二选一） |
| Iozone | 综合文件 IO | 可交叉 | 可 | **不做**（与 fio 高度重叠） |
| Phoronix Test Suite | 全栈综合 | 需要大量运行时依赖、网络下载 | 不适合板子 | **不做** |
| UnixBench | 老式系统综合 | 脚本 + C | 可 | **不做**（结果口径模糊，多数已被 LMBench/sysbench 覆盖） |
| JetStream / Speedometer | 浏览器 JS | 浏览器只有 Firefox ESR | 可 | **可选**（GUI 回归，非性能门禁） |

## 3. 测试矩阵

分三层，逐层收敛：

### L1 微基准（QEMU 与板子都跑，秒级~分钟级）

| 域 | 用例 | 主要指标 |
| --- | --- | --- |
| syscall | `getpid` `getuid` `clock_gettime` `write(/dev/null)` `open/close` | ns/op |
| 进程 | `fork+exec+wait`、`fork+exit`、`pipe` 往返、`socketpair` | µs/op |
| 调度 | hackbench（多组进程/线程）、schbench 唤醒延迟 | s、µs |
| 内存 | STREAM copy/scale/add/triad、mbw memcpy | MB/s |
| 存储 | fio：4K 随机读/写（QD1、QD32）、128K 顺序读/写、fsync 延迟分布 | IOPS、µs、MB/s |
| 元数据 | fs-mark：create/delete/stat/rename 混合 | ops/s |
| 网络 | iperf3 TCP 单流/多流、UDP；连接建立时延 | Gbit/s、ms |

### L2 系统级场景

- **Debian 热路径**：`apt-get update`（若离线则用固定包列表的 `dpkg -i`）、`tar` 解压、
  大目录 `find`；
- **桌面启动**：从 `booti` 到 `ASTERINAS_DESKTOP_DISPLAY_READY` 的墙钟时间；
- **浏览器**：Firefox 加载固定本地页面（避免网络抖动）到 `load` 事件。

### L3 对照与判定

- 每个用例在 **Asterinas 板端** 与 **Linux 板端**（RockOS 启动同一份 Debian rootfs）
  各跑 5 样本，取 median/p95；
- 门禁不设绝对阈值，判 **ratio（Asterinas / Linux）** 与历史基线的回退幅度；
- 所有数字必须带 provenance（内核 commit、rootfs 校验和、QEMU/板子序列号）。

## 4. 实施步骤

1. **补齐工具链**（半天）：sysbench、STREAM、fs-mark、hackbench/schbench、iperf3 的
   riscv64 交叉编译脚本，统一放进 `tools/riscv/perf/`，产物落到 `target/bench/`；
2. **板端采集脚本**（半天）：一个 `bench_matrix.sh` 在板上串行跑完矩阵并输出
   `PERF_SAMPLE` 行，主机侧用 `collect_samples.py` 通过 SSH 采集；
3. **Linux 对照**（半天）：同一块板子用 RockOS 启动 + 同一份 rootfs，跑同一脚本
   （rootfs 里已有 Debian 工具）；
4. **QEMU 对照**（已有基础）：`virt` + 4 vCPU + 2 GiB + virtio-blk/net，作为无硬件
   时的快速回归；
5. **接入门禁**：先用 `aggregate_results.py` 固化第一版基线，再给 2~3 个关键指标
   （syscall、fio 4K 随机读、fork+exec）设回退阈值。

## 5. 已知风险

| 风险 | 处理 |
| --- | --- |
| 板子存储是 SD/eMMC，容量与寿命有限 | fio 限定 1 GiB 文件、限制总写入量；避免反复大文件写 |
| 校园网抖动影响网络指标 | 网络用例固定打主机 `iperf3 -s`，同一交换机 |
| 板子时钟不稳（无 RTC 电量） | 所有计时用 `clock_gettime(CLOCK_MONOTONIC)`，不依赖墙钟 |
| 硬重启会丢未落盘数据 | 每组测量前 `sync`，组间可重启；结合 ext2 写序修复 |
| 交叉编译依赖缺失 | 优先纯 C、无 autotools 的项目；`filebench` 因此暂缓 |
