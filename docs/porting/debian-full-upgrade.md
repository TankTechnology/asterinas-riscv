# Debian 跨版本升级（L6：trixie → forky）

## 结论摘要

**2026-10-25 终验（内核 `e23120606e4d942f`，HEAD `99d4b116e`）：升级完成，`allocation failed` 端到端为 0，L6 通过。**

- `full-upgrade` **rc=0**，耗时 **4825.30 s**：**296 upgraded / 45 newly
  installed / 0 to remove / 0 not upgraded**；**341/341** 解包、
  **341/341** 配置；
- **端到端零分配失败**：`allocation failed` **0** 条（含 `size=65536`）、
  `segment allocation failed` **0**、`segmented fallback` **0**；
  ring-buffer 池 **33 命中 / 0 未命中**，heap 池峰值 **2685/4096 页**、
  **0** 次耗尽；无 panic / heap error；
- 二次启动：`debian_version=forky/sid`、systemd **running**、串口登录成功；
- 升级后根文件系统剩余 **1,064,400 KB**（高于 800 MiB 护栏）；基础与
  副本 sha 相同，基线未被修改；
- 关键修复链（每步都有独立提交与证据）：
  1. ext4 日志事务跨 descriptor（`14b2d352a`）→ 消除 E2BIG；
  2. 4 KiB 缓存回填失败降级（`42c675571`）→ 消除逐页饥饿；
  3. 内核栈 4 页分段（`f67945641`）→ 消除栈的 order-4；
  4. 分配器级分段回退（`dbd8079df`）→ 统一处理 64 KiB 请求；
  5. heap 大槽 16 MiB 启动预留池（`abe82e29`）→ 首次跑完的关键：
     池峰值 **2404/4096 页**、1,293,312 次大槽分配、0 次耗尽；
  6. ring-buffer 增长改走同一预留池（`99d4b116e`，保留 4 MiB 给 heap）
     → 终验关键：64 KiB 请求不再触碰 frame allocator；
- 剩余噪声：negative dentry 警告 46,643 条（无淘汰）；page cache 仍无
  压力回收（升级完成时约 1.4 GiB 空闲）；
- **历史记录**：2026-10-24 及更早，本路径曾依次被 ext4 E2BIG、时间预算、
  帧分配 OOM（order-7 → order-4 → 4 KiB）阻塞；下文“结果”“失败归因”
  等节保留这些结论作为演进证据，不再代表当前状态。

## 通过轮的环境与两步归因（2026-10-25）

### 环境

| 项 | 值 |
|---|---|
| 内核 | `target/osdk/asterinas-osdk-bin.elf`，sha256 `e23120606e4d942f…`（HEAD `99d4b116e`） |
| 内核配置 | x86_64、`--scheme microvm`（vmm-direct）、4 vCPU、4 GiB、KVM |
| Stage1 initramfs | `target/debian-x86/stage1/initramfs.cpio`（sha256 `fb9c5b17…`） |
| 基础镜像 | `target/systemd-health/daily.ext2`（sha256 `450ebe3b…a083e0`） |
| 工作副本 | `target/debian-upgrade-final/daily.ext2`（sha 与基础相同） |
| 网关 | `make test_x86_debian_full_upgrade`，90 分钟预算 |
| 耗时 | **4825.30 s**（约 80.4 分钟；预算内） |
| 结果 | `stages=5 ok=5 fail=0`；`upgrade rc=0`；末尾 `Powering off.` |
| 证据 | `docs/porting/evidence/2026-10-25-l6-final-zero-failures/` |

### 两步归因（把 OOM 从 order-7 推到“能跑完”）

> 终验在此之上再加一步：ring-buffer 增长改走同一预留池（`99d4b116e`），
> 把最后的 64 KiB 连续失败清零（33 命中 / 0 未命中）。

1. **内核栈多段化（16 页 → 4 页/段，order 4 → order 2）**
   `ostd/src/task/kernel_stack.rs`：把 128 页内核栈拆成 32 段 × 4 页，
   虚拟连续、物理分段。它消除了 `spawn task failed` 的 order-7 与
   order-4 失败点（3340 s → 3578 s 的两次 panic 都由此而来）。
2. **内核堆 16 MiB 启动预留连续池**
   `ostd/src/mm/heap/slot.rs` + `ostd/src/lib.rs`：启动早期预留 16 MiB
   连续物理内存，heap 大槽（36–60 KiB）全部从池内 first-fit 分配，
   不再向碎片化的 buddy 要 order-4；池峰值 **2404/4096 页**、
   **1,293,312** 次分配、**0** 次耗尽。这是本轮从“反复挂起”变成
   “升级跑完”的决定性一步。

### L4 / L5 回归（同内核 `abe82e29`，不重跑 L6）

| 关卡 | 结果 | 与基线比较 |
|---|---|---|
| L4 源码包构建 | 3 包 **2 ok / 1 fail**：`hello`（2 个 .deb）、`python3-minimal`（19 个 .deb）成功；`coreutils` 在 `debian/rules clean` 被 signal 11 | **一致**（coreutils 是文档中已记录的已知失败） |
| L5 从零 rootfs | `stages=3 ok=3 fail=0`（install/foreign/second）；chroot/mount/umount/unshare 四个 probe 全 **rc=0**；`debian_version=13.7`，`/target` 持久化 | **一致**（全绿基线） |

→ 16 MiB 启动池与分段回退没有引入 L4/L5 退化。

### 剩余问题

- **ring-buffer 64 KiB 已收尾**（`99d4b116e`）：终验中 **33 次池命中 /
  0 次未命中**，64 KiB 请求不再触碰 frame allocator，`allocation failed`
  为 0；
- negative dentry 无淘汰（46,643 条警告）、page cache 无压力回收；
  升级完成时约剩 1.4 GiB 空闲，余量不宽裕。

## 安全前置

| 项 | 值 |
|---|---|
| 基础镜像 | `target/systemd-health/daily.ext2` |
| 基础镜像 sha256 | `450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0` |
| 升级副本 | `target/debian-upgrade/daily.ext2` |
| 副本 sha256 | `450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0` |
| 复制耗时 | 0.45 s（`cp --sparse=always`） |
| 基础镜像是否被修改 | **否**（升级前后 sha 相同，所有写入只落在副本上） |

> 说明：L1–L5 文档引用的 `target/final-matrix/xwrap/image18.ext2` 已不在
> `target/` 中（本轮开始前被清理）。本轮改用同源的 L2 工作副本
> `target/systemd-health/daily.ext2`：trixie 13.7、`sources.list` 相同
> （仅 `trixie main`）、`e2fsck -fn` 干净、空闲 2.4 GiB。基础与副本的
> sha 都在 `target/debian-upgrade/upgrade/copy.txt` 里。

## 方法

- guest 探针：`tools/x86/debian/full_upgrade.sh`（注入到
  `/usr/local/bin/full-upgrade`），由 `daily_use_gate.sh --full-upgrade`
  在串口登录后以 root 执行；
- 五个阶段：

  | 阶段 | 在哪 | 动作 |
  |---|---|---|
  | copy | host | `cp --sparse=always` 基础镜像 → `target/debian-upgrade/daily.ext2`，记录双 sha |
  | source | guest | `sed -i s/trixie/forky/g`（`.list` 与 `.sources`） |
  | update | guest | `apt-get update -o Acquire::AllowReleaseInfoChange=true` |
  | upgrade | guest | `DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Lock::Timeout=300 full-upgrade`，上限 40 分钟 |
  | reboot | host | 对**同一个副本**再次启动，串口登录后执行 `cat /etc/debian_version` 与 `sudo systemctl is-system-running` |

- 串口协议：`UPG stage=<...> rc=<n> dur=<s>`、`UPG disk_free_kb=<n>`、
  `UPG err=<摘要>`、`UPG_DONE`；host 侧另加 `stage=copy` 与 `stage=reboot`；
- 磁盘护栏：每个 guest 阶段前后采样根文件系统；低于 **800 MiB**
  （819,200 KB）立即记 `disk` 并停止；
- 升级参数：`-o Dpkg::Options::=--force-confold`（无人值守，保留旧配置）；
- 失败后**不修复**副本，只做二次启动判定并如实记录。

## 环境

| 项 | 值 |
|---|---|
| 日期 | 2026-10-24 |
| 架构 / 加速 | x86_64 / KVM（4 vCPU、4 GiB） |
| 宿主 HEAD | `0d5b7260e`（L5 最小复现提交；L6 改动见本文件所在提交） |
| 内核 | `target/prime/kernels/x86.elf`，sha256 前缀 `9e8ee5dd544fa03a` |
| Stage1 initramfs | `target/debian-x86/stage1/initramfs.cpio`，sha256 前缀 `fb9c5b1765911cf0` |
| 基础镜像 | `target/systemd-health/daily.ext2`（见“安全前置”） |
| 起始发行版 | Debian trixie（13.7） |
| 目标发行版 | Debian forky（testing） |
| 镜像源 | `https://mirrors.tuna.tsinghua.edu.cn/debian` |
| 起始根文件系统空闲 | 2,308,936 KB（约 2.2 GiB） |
| 宿主磁盘空闲 | 约 21.1 GB |

## 结果（五阶段）

| 阶段 | rc | 分类 | 耗时 | 关键数据 |
|---|---|---|---|---|
| copy | **0** | ok | 0.45 s | base/copy sha 相同：`450ebe3b…a083e0` |
| source | **0** | ok | 0.08 s | `deb https://mirrors.tuna.tsinghua.edu.cn/debian forky main` |
| update | **0** | ok | 12.29 s | **297** 个包可升级 |
| upgrade | **100** | **磁盘** | 143.09 s | 计划 296 upgraded + 45 newly installed；下载 **341** 个包；实际解包 **8**、配置 **4**；在 `linux-libc-dev` 处中止 |
| reboot | **0** | ok | 8.85 s | 串口登录成功；`debian_version=13.7`；`systemctl is-system-running=running` |

升级中止时已完成的包（从 `/var/tmp/upgrade/upgrade.log`）：

```
Unpacking (8):
  rpcsvc-proto, libc-dev-bin, libcrypt1, libcrypt-dev, libc6-dev,
  libc6 (2.43-7, from forky), libc-gconv-modules-extra, libc-bin
Setting up (4):
  libcrypt1 (1:4.5.2+20251210-1), libc-gconv-modules-extra (2.43-7),
  libc6 (2.43-7), libc-bin (2.43-7)
```

关键错误原文（串口 `UPG log upgrade=` 与 `UPG err=`）：

```
dpkg: error processing archive /var/cache/apt/archives/linux-libc-dev_7.2.11-1_all.deb (--unpack):
unable to sync file /usr/include/asm-generic/auxvec.h.dpkg-new : Argument list too long
dpkg: unrecoverable fatal error, aborting:
unable to fsync updated status of linux-libc-dev: Argument list too long
E: Sub-process /usr/bin/dpkg returned an error code (2)
E: Unable to synchronize mmap - msync (7: Argument list too long)
```

升级后的副本（部分升级）：

- 串口登录：**成功**（client `user=True pass=True command=True done=True`）；
- systemd：**`running`**（`sudo systemctl is-system-running`）；
- `/etc/debian_version`：**13.7**（仍是 trixie；`base-files` 未升级）；
- 值得注意的是：forky 的 **glibc 2.43** 已经替换并配置成功，副本仍能启动
  到 systemd——说明 libc 本身在这版内核上可用，阻塞点不是 libc。

## 时间预算（40 分钟 vs 90 分钟）

修复 ext4 事务上限后，L6 的失败从 E2BIG 变成时间预算；把探针的
`UPG_UPGRADE_TIMEOUT` 从 2400 s 提到 **5400 s**（90 分钟）重跑同一镜像：

| 预算 | 结果 | 解包 / 配置 | 结束原因 | 重启后 |
|---|---|---|---|---|
| 40 min（2400 s） | rc=124（timeout） | **227 / 341**，配置 78 | 阶段超时 | `forky/sid`，systemd running |
| 90 min（5400 s） | **内核 panic（无 rc）** | **321 / 341**，配置 78 | `spawn task failed: NoMemory`（guest **3427.5 s**，约 57 min） | `forky/sid`，systemd running，串口可登录 |

90 分钟轮的完整证据：

- 复制：base/copy sha 相同（`450ebe3b…`），0.53 s；source 0.05 s；
  update 12.17 s（297 可升级）；
- upgrade 阶段没有返回 rc：在 guest 3427.5 s 时内核 panic；
- 升级日志（`/var/tmp/upgrade/upgrade.log`）在崩溃前有 **321 条
  `Unpacking`**、**78 条 `Setting up`**，最后停在
  `Unpacking libxcursor1 …`；
- 同轮 `ext4 journal transaction is too large` **0 条**——E2BIG 没有回来；
- panic 原文：

  ```
  Uncaught panic:
      spawn task failed: NoMemory
      at kernel/core/src/thread/task.rs:135
  ```

- panic 前串口有 **75,969** 条
  `WARN: number of negative dentries has reached …`；guest 内存 4 GiB；
- 崩溃后对同一副本做二次启动：journal 回放成功，登录提示正常，
  `sudo systemctl is-system-running` = **running**，
  `debian_version` = **forky/sid**。

归因：**内存**（negative dentry/page cache 增长耗尽内核内存），不是
E2BIG、不是 dpkg 冲突/依赖、也不是 90 分钟超时——panic 发生在 57 分钟，
离 90 分钟上限还早。时间预算已经足够，限制变成了 guest 内存与 dentry
缓存策略。panic 的类型、栈顶子系统与三条可验证假设见
[`2026-10-24-l6-panic-attribution.md`](2026-10-24-l6-panic-attribution.md)。

## 失败归因

| 阶段 | rc | 分类 | 证据 |
|---|---|---|---|
| upgrade | 100 | **磁盘**（ext4 日志事务上限） | `Unable to synchronize mmap - msync (7: Argument list too long)`；`unable to sync file …auxvec.h.dpkg-new : Argument list too long`；`unable to fsync updated status of linux-libc-dev: Argument list too long` |

不是以下类别：

- **dpkg 冲突**：没有 `trying to overwrite`、conffile 冲突或
  `dpkg: error: ... conflict`；中止前 4 个包的配置均成功；
- **依赖不满足**：`apt-get update` 后依赖解析完成，计划（296+45）已生成，
  341 个包全部下载成功；
- **syscall 缺失**：错误是 `E2BIG`（`Argument list too long`），来自文件
  系统写回，不是 `ENOSYS`；同轮没有 `Function not implemented`；
- **超时**：143.09 s，远低于 40 分钟上限；
- **启动失败**：副本二次启动成功（systemd running、串口可登录）。

根因与 L4/L5 相同：ext2/ext4 驱动的 JBD2 事务编码器每个 descriptor 最多
**253 个元数据块**（`kernel/core/src/fs/fs_impls/ext2/journal.rs:233-238`）。
`linux-libc-dev` 解包时 `dpkg` 的 `msync`/`fsync` 强制提交一个超过容量的
元数据集合，于是返回 E2BIG。`linux-libc-dev` 本身有大量头文件，正是
“一个包就制造出大量脏元数据”的典型。L5 已给出不跑 debootstrap 的最小
复现（12000 个空文件 + `ldconfig`）。

## 评级与结论

- **升级路径可用性：可用**（2026-10-25，内核 `abe82e29`）；
- 分数意义上的评级：**源/网络/依赖 5/5，执行 5/5**——切源、`apt-get
  update`、341 个包下载、解包、配置、升级后二次启动全部通过；
- 升级后的系统确实是 forky/sid（`debian_version=forky/sid`），systemd
  running，串口可登录；
- 仍然存在的工程债：ring-buffer 的 64 KiB 依赖分段回退（可继续用池/
  4 KiB 段优化）、negative dentry 无淘汰、page cache 无压力回收；
  升级在 1.46 GiB 空闲下完成，余量不算宽裕。

## 复现方式

```sh
make test_x86_debian_full_upgrade \
  X86_DEBIAN_KERNEL=$PWD/target/prime/kernels/x86.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=$PWD/target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/systemd-health/daily.ext2 \
  FULL_UPGRADE_WORKSPACE=$PWD/target/debian-upgrade
```

整轮墙钟约 3 分钟（含复制、两次启动）；升级阶段自身 143 s。

产物：`target/debian-upgrade/console.txt`（第一次启动全量串口）、
`reboot-console.txt`（二次启动）、`upgrade/stages.tsv`、`upgrade.json`、
`upgrade/copy.txt`（双 sha）。前两轮（含一次标记回显导致的假 reboot）
的原始记录保留在 `target/debian-upgrade/run1/`、`run2/`。

## 基线声明

- **基础镜像 sha256**：`450ebe3bf2506c290902d55ae651ba796e67c0b9a846c528de34528f82a083e0`
  （L2 工作副本；原 `image18.ext2` 已被清理）；
- **副本 sha256**：与基础相同（升级在副本上进行，基础未改）；
- **五阶段**：copy **0** / source **0** / update **0** / upgrade **100** /
  reboot **0**；
- **已知失败**：`upgrade`，`linux-libc-dev` 解包，`E2BIG`（ext4 日志）；
- **判定规则**：若内核修复事务容量后重跑，upgrade 应转为 rc=0，且
  reboot 的 `debian_version` 应变成 forky（14.x）；若 copy 的
  base/copy sha 不再相同，说明基础镜像被改，属于门禁故障。
