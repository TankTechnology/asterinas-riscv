# Debian guest 30 轮内存/稳定性压测（512 MiB/轮）

日期：2026-10-25　HEAD：`db20ab5ea`
内核：`target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a`
镜像：`target/debian-x86-gtk2/rootfs/debian-root.ext2`（1.1 GiB，357 包）
环境：x86_64 / KVM，QEMU 4 GiB；启动到登录提示 ≈ **3.3 s**（console 内核时间戳
`[3.305]` 后紧接 `login:`）。

## 结论摘要

- **判定：稳定**。30 轮 512 MiB 压力后，`dmesg` 中 `Uncaught panic` /
  `oom-killer` / `Out of memory` / `allocation failed` **全部为 0**。
- **无泄漏信号**：`MemAvailable` 3,975,440 → 3,936,352 kB，
  **Δ = -39,088 kB（-0.983%）**，远小于 5% 阈值。
- **gcc 编译压力未生效**：30/30 轮 `gcc_rc=127`（`gcc: not found`），
  镜像剖面没有编译器；`python3` 内存压力 30/30 轮成功（0 失败）。
- 实际墙钟约 **7.8 分钟**（final block 时 guest uptime 480 s，扣登录前 ~4 s）；
  脚本里的 `t=Nmin` 是**轮次编号**，不是分钟。

## 1. 方法

沿用 daily 门禁的启动/登录/串口流程，把 guest 端 checklist 换成压测脚本：

```sh
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/stress_memory.sh \
make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/debian-x86-gtk2/rootfs/debian-root.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm X86_DEBIAN_XORG_TIMEOUT=1800 \
  DAILY_USE_WORKSPACE=$PWD/target/stress
```

每轮（共 30 轮）：

1. **内存压力**：`python3` 分配 512 MiB `bytearray`，每 4 KiB 写一页，sleep 5 s；
2. **编译压力**：`gcc -O2` 编译并运行一个最小 C 程序（上限 40 s）；
3. **采样**：读 `/proc/meminfo` 的 `MemAvailable` / `Cached`，sleep 10 s。

脚本把每轮写入 guest 的 `/tmp/stress.log` 并同步 `tee` 到 `/dev/console`；
结束时打印完整 `stress.log`、失败计数、`dmesg` 四类标记扫描与 `dmesg` 尾部。

> 说明：任务书的脚本以 `sleep 10`、30 轮运行，因此实际压测约 8 分钟，而不是
> 字面 30 分钟；`t=10/20/30min` 是第 10/20/30 轮。若需要真正的 30 分钟持续
> 压力，应把每轮 sleep 提到 ~50 s（本轮按任务书原样执行，未改）。

## 2. 原始日志片段

guest `/tmp/stress.log`（经串口打印，节选）：

```
baseline_memavailable_kb=3975440
t=10min memavail=3956440 caches=
t=20min memavail=3944952 caches=
t=30min memavail=3935664 caches=
stress_failures python3=0/30 gcc=30/30
baseline_memavailable_kb=3975440 last_memavailable_kb=3936352
dmesg_count[Uncaught panic]=0
dmesg_count[oom-killer]=0
dmesg_count[Out of memory]=0
dmesg_count[allocation failed]=0
```

前 3 条错误（全部来自不存在的 gcc）：

```
round=1 gcc_rc=127 err=sh: 1: gcc: not found
round=2 gcc_rc=127 err=sh: 1: gcc: not found
round=3 gcc_rc=127 err=sh: 1: gcc: not found
```

补充：`caches=` 为空是**字段不存在**，不是采样失败。活树
`kernel/core/src/fs/fs_impls/procfs/meminfo.rs` 只输出
`MemTotal/MemFree/MemAvailable/SwapTotal/SwapFree/Slab`，并在注释中说明
“当前没有可回收页，MemFree 与 MemAvailable 相同”。

## 3. 三问判读

### 3.1 panic / OOM / allocation failed

| 标记 | 次数 |
|---|---|
| `Uncaught panic` | **0** |
| `oom-killer` | **0** |
| `Out of memory` | **0** |
| `allocation failed` | **0** |

### 3.2 内存泄漏

- baseline `MemAvailable = 3,975,440 kB`
- 末次 `MemAvailable = 3,936,352 kB`
- Δ = **-39,088 kB（-0.983%）** → **远低于 5% 阈值，无泄漏信号**。
- 中途最低点 t=30 采样为 3,935,664 kB，整条曲线单调缓降约 1%，与
  python3 解释器/页表/文件缓存占用相符；没有逐轮持续下滑的形态。

### 3.3 失败任务数

- `python3` 内存压力：**0/30** 失败（每轮正常分配、触碰并退出）；
- `gcc` 编译压力：**30/30** 失败，原因统一为 `sh: 1: gcc: not found`
  （镜像的 `desktop-drm` 剖面只带 `gcc-14-base` 运行库，没有编译器）。
  这属于**压测环境缺口**，不是内核不稳定。

## 4. 判定

**稳定**：有内存压力（30 × 512 MiB）下无 panic、无 OOM killer、无内核分配失败，
可回收内存回到基线的 99% 以内。

限制与后续：

1. 编译压力未覆盖——把 `gcc` 加入剖面（或改用已装的工具做 CPU/编译压力），
   再跑同样的 30 轮；
2. 若目标是字面 30 分钟持续压力，把每轮 `sleep 10` 调到 ~50 s；
3. `/proc/meminfo` 缺 `Cached/Buffers`，泄漏判断只能用 `MemAvailable`
   （当前实现中它等于空闲帧数，不含可回收 page cache）；如需更细的水位曲线，
   可另行补字段。

## 5. 证据与门禁结果

- `target/stress/console.txt`（串口全量）、`target/stress/host.log`；
- 归档：`docs/porting/evidence/2026-10-25-stress-memory/console-excerpt.txt`；
- 门禁 `GATE_EXIT=2` 的原因：自定义 checklist 不输出标准 `DAILY_<域>=ok`
  行，门禁据此报 `no result for: DESKTOP, APP, …`；压测本身已完成
  （`DAILY_CLIENT ... done=True`、`DAILY_DONE=ok`）。
