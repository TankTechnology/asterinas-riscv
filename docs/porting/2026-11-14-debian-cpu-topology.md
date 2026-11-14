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

## 4. 已知限制

`lscpu -e` / `lscpu -p` **仍然输出空行**（CPU 列是 `-`）。排查记录：

- `possible/online/present` 都正确解析，`taskset`/`nproc`/`sched_getaffinity`
  都报 4 核、掩码 `1111`；
- sysfs 的 `readdir` 返回正确的 `d_type`（`DT_DIR`），`O_PATH`/`O_DIRECTORY`/
  `openat` 全部可用；
- 用 `LD_PRELOAD` 拦截 `open/openat/stat/fstatat/opendir/fopen` 观察 lscpu：
  它只打开了 `/sys/devices/system/cpu` 目录本身，**从未打开任何 `cpuN/` 下的文件**，
  所以那些列本来就是空的。

结论：缺的是它枚举 CPU 所用的某个本文档尚未覆盖的入口，默认输出（脚本真正解析的
那一行 `CPU(s): 4`）是对的。要继续查需要 util-linux 2.41 的枚举逻辑。

## 5. 仍未做

- `/proc/interrupts`、`/sys/module`、`/proc/sysvipc`、`/proc/cgroups`、`/proc/pressure`。
- `lscpu` 的 "Model name"/"Vendor ID" 依赖 `/proc/cpuinfo` 里的对应字段，本内核
  没有输出（RISC-V 上并非必需，但会让 lscpu 少几行）。
