# 让 `lscpu` 能描述 CPU（2026-11-14）

## 1. 缺口

`lscpu` 的第一行就失败：

```
lscpu: failed to determine number of CPUs:
       /sys/devices/system/cpu/possible: No such file or directory
```

`/sys/devices` 整个目录都不存在（`/sys/dev` 是设备号目录，不是一回事），
CPU 拓扑无处可寻。

## 2. 新增的树

```
/sys/devices/system/cpu/
├── possible / present / online   "0-3"
├── offline                       ""（空行，全部在线时 Linux 也这么写）
├── kernel_max                    "63"
└── cpu0..cpu3/
    ├── online                    "1"
    └── topology/
        ├── core_id               0..3
        ├── physical_package_id   0（单封装）
        ├── thread_siblings{,_list}   只有自己（无 SMT）
        └── core_siblings{,_list}     全部 CPU
```

二进制 cpumask 与 `_list` 文本两种形式都给：新版 util-linux 读 `_list`，
旧版只认二进制掩码。

本板的核是相同核心、无 SMT、单封装，所以"同线程兄弟=自己、同核兄弟=全体"这一定义
是准确的，不是占位值。

## 3. 验证

**QEMU 先行**（`tools/riscv/perf/probes/cpuprobe.c`，`-smp 4`）：

```
ok   possible exists
ok   possible parses as a CPU list: 0-3
ok   present / online / offline / kernel_max exist
ok   one directory per possible CPU
ok   each core_id matches its directory name
ok   sysfs, cpuinfo and stat agree on the CPU count: sysfs=4, /proc/cpuinfo=4, /proc/stat=4
ok   online lists every CPU: 0-3
ok   topology files are readable
CPUPROBE OK
```

三方一致性是关键一项：sysfs、`/proc/cpuinfo`、`/proc/stat` 必须报同一个核数，
否则读不同文件的工具会互相矛盾。

**真机**（同一探针 11/11）上：

```
$ lscpu
Architecture:        riscv64
Byte Order:          Little Endian
CPU(s):              4
On-line CPU(s) list: 0-3
```

## 4. `lscpu -e` / `-p` 空行的真正原因与修复

最初 `lscpu -e` 每行都是 `-`。排查走了三步：

1. `possible/online/present` 解析正确，`nproc`/`sched_getaffinity` 都报 4 核；
2. sysfs 的 `readdir` 返回正确的 `d_type`，`O_PATH`/`openat` 可用；
3. 用 `LD_PRELOAD` 拦截 `open/openat/fstatat/opendir/fopen` 观察 lscpu 的实际调用：
   它读了 `kernel_max/possible/present/online` 和 `/proc/cpuinfo`，**却从未打开任何
   `cpuN/` 下的文件**。

第 3 步把范围缩小到 lscpu 内部逻辑，于是去读 util-linux 2.41 源码，找到
`lscpu-topology.c`：

```c
if (!cpu || cpu->type != ct)
    continue;          /* 每个核的拓扑只有在其 CPU 类型已知时才读 */
```

CPU 的 `type` 由 `/proc/cpuinfo` 的**身份字段**建立。RISC-V 的对应字段是
`mvendorid` / `marchid` / `mimpid` / `uarch`（见 `lscpu-cputype.c` 的模式表），
而我们的 cpuinfo 只有 `processor` / `isa` / `mmu`，于是一个 type 都没有，
所有核被跳过。

修复：`uarch` 从设备树 hart 节点的 `compatible` 取（Linux 在无法把 CSR 映射到已知
核心时也用它），只取属性列表的第一个字符串。前三个是机器态 CSR，S 态不保证可读，
所以不用它们。

修复后板上：

```
$ lscpu
Architecture:        riscv64
Byte Order:          Little Endian
CPU(s):              4
On-line CPU(s) list: 0-3
Model name:          eswin,eic770x
Thread(s) per core:  1
Core(s) per socket:  4
Socket(s):           1

$ lscpu -e
CPU SOCKET CORE ONLINE
  0      0    0    yes
  1      0    1    yes
  2      0    2    yes
  3      0    3    yes

$ lscpu -p
# CPU,Core,Socket,Node
0,0,0,
1,1,0,
...
```

## 5. 仍未做

- `/proc/interrupts`、`/sys/module`、`/proc/sysvipc`、`/proc/cgroups`、`/proc/pressure`。
- `Vendor ID` 仍缺：它来自 `mvendorid`（机器态 CSR，S 态不保证可读）。
  只影响一行展示，不影响任何工具的解析。
