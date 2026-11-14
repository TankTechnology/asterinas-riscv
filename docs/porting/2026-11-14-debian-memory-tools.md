# 让 `vmstat` / `sar` 能读内存（2026-11-14）

## 1. 缺口

`vmstat` 直接报错：

```
vmstat: Unable to create vmstat structure
```

原因是 `/proc/vmstat` 不存在——procps 的 `vmstat` 打不开它就不往下走。同批缺的还有
`/proc/zoneinfo`（`sar -r`、监控面板）、`/proc/buddyinfo`（碎片观察）、
`/proc/pagetypeinfo`。

## 2. 加的四个文件

| 文件 | 内容 | 数据来源 |
| --- | --- | --- |
| `/proc/vmstat` | `key value` 表，vmstat 会读的键**全部给出** | 见下 |
| `/proc/buddyinfo` | 单个 zone 的空闲块直方图（按 order） | 帧分配器空闲链遍历 |
| `/proc/zoneinfo` | 单 zone 的 `pages free`/`spanned`/`present`/`managed` | 同上 + `mem_total()` |
| `/proc/pagetypeinfo` | 与 buddyinfo 同源，按迁移类型分列 | 全部记作 `Unmovable` |

`/proc/vmstat` 里哪些是**真数据**：

- `nr_free_pages` —— 分配器的精确空闲总量；
- `pgfault` / `pgmajfault` —— 新增系统级计数器，挂在
  `IoAccounting::account_page_fault()`（所有缺页的唯一入口）；
- `pgpgin` / `pgpgout` —— 块层 devicestats 的扇区数 ÷ 8
  （比 Linux 的"页缓存换页"口径宽，凡是走过块层都算）；
- `nr_free_chunks` —— 与 buddyinfo 的求和一致。

其余键（slab、回收、扫描、compaction、swap 等）一律输出 0：内核不统计这些事件,
**缺少键比输出 0 更糟**——很多工具把"键不存在"和"值为 0"当作两回事。

## 3. 分配器侧改动

`osdk-frame-allocator` 新增只读接口：

```rust
pub const BUDDY_ORDERS: usize = 32;
pub fn load_free_chunks_by_order() -> [usize; BUDDY_ORDERS]
```

实现是遍历每个 CPU 的本地池（`LOCAL_POOL.get_on_cpu(cpu)`）和全局池的每条空闲链。
**加锁顺序与分配器一致**（一次只持有一个本地池，最后取全局池），所以与正在进行的
分配不会形成环。实测在 10 GiB 板上耗时 **31 ms**，属于诊断路径可以接受的范围。

## 4. 验证

**QEMU 先行**（`tools/riscv/perf/probes/vmprobe.c`，静态二进制当 init）：

```
ok   /proc/vmstat carries the keys vmstat reads
ok   /proc/buddyinfo parses as one zone of free pages
ok   /proc/meminfo reports MemFree
ok   buddyinfo adds up to MemFree: buddy=503946 pages, MemFree=504058 pages, delta=112
ok   /proc/zoneinfo reports pages free
ok   /proc/zoneinfo reports spanned pages
ok   zoneinfo agrees with buddyinfo: zoneinfo=504058, buddy=503946
ok   /proc/pagetypeinfo has a page block header
ok   /proc/pagetypeinfo lists the migrate types
VMPROBE OK
```

**交叉校验**是关键的一项：把 buddyinfo 每列 `count << order` 求和，与
`/proc/meminfo` 的 `MemFree` 对比。差值来自两次读取之间的分配抖动（0.02–0.04%），
说明按 order 的计数是**准确的**，不是凑出来的。

**真机**（同一探针 9/9，delta 962 / 2,209,137 页）：

```
$ vmstat
procs -----------memory---------- ---swap-- -----io---- -system-- -------cpu-------
 r  b   swpd   free   buff  cache   si   so    bi    bo   in   cs us sy id wa st gu
 2  0      0 8865272      0      0    0    0  1085   111 18235    2 29 41 30  0  0  0

$ vmstat -d
disk- ------------reads------------ ------------writes----------- -----IO------
       total merged sectors      ms  total merged sectors      ms    cur    sec
loop0      0      0       0       0      0      0       0      0      0      0

$ cat /proc/buddyinfo
Node 0, zone   Normal 27241 11167 6778 1 1 2 1 1 1 0 0 0 0 1 0 1 2 3 2 2 0 ...
```

`free` 8865272 kB = 2,216,318 页，与 zoneinfo 的 `pages free` 一致。

## 5. 仍然缺的（不属于本项）

- `/sys/devices/system/cpu/**` → `lscpu`、`htop`、`nproc`（原计划第 2 项）。
- `/proc/interrupts`、`/sys/module`、`/proc/sysvipc`、`/proc/cgroups`、`/proc/pressure`。
