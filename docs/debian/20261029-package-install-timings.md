# 2026-10-29 包安装耗时表（T3b）：实测值、失败样本与 ext4 日志事务上限

- 仓库：`~/Program/asterinas-riscv`，HEAD `7564b2976`（main，工作树有与本任务无关的改动）
- 目标线：x86_64 QEMU（KVM）门禁环境里的 Debian trixie 客户机
- 纪律：不 push、不改内核代码、不伪造耗时
- 结果：**客户机正常启动，四包全部产生了实测数据**。`git`/`curl`/`python3` 在镜像里已安装，只能测
  `mode=reinstall`（下界）；`vim` 是真正的首次安装，**3 次里成功 1 次（99.00 s），另 2 次在 16–22 s 处
  失败**，失败原因是内核 ext4 日志单个事务的上限（E2BIG），不是超时或宿主权限。

## 1. 先给结论

四个目标包在本镜像上的实测（同一内核、同一镜像、KVM、4 vCPU / 4 GiB）：

| 包 | 模式 | 运行 | 结果 | 总耗时 s | 解包 s | 配置 s | 下载 |
|---|---|---|---:|---:|---:|---:|---|
| `git` | `reinstall` | run1 | 成功 | **45.31** | 35 | 0 | 8862 kB / 1 s |
| `git` | `reinstall` | run3\* | 成功 | 50.91 | 40 | 0 | 8862 kB / 1 s |
| `curl` | `reinstall` | run1 | 成功 | **7.35** | 1 | 0 | 270 kB / 0 s |
| `curl` | `reinstall` | run3\* | 成功 | 7.74 | 1 | 0 | 270 kB / 0 s |
| `vim` | `fresh` | run2 | 成功 | **99.00** | 87 | 2 | 9397 kB / 1 s |
| `vim` | `fresh` | run1 | **失败** | 22.10 | 未测（脚本正则缺陷） | — | 9397 kB / 1 s |
| `vim` | `fresh` | run3\* | **失败** | 24.30 | 16（中断前的部分解包） | — | 9397 kB / 1 s |
| `python3` | `reinstall` | run4 | 成功 | **13.01** | 1 | 1 | 28.2 kB / 0 s |
| `python3` | `reinstall` | run1 / run3 | **失败** | 2.59 / 3.04 | — | — | 无（被中断状态挡住） |
| `sl`（锚点） | `fresh` | run2 | 成功 | **13.12** | 2 | 1 | 118 kB / 0 s |
| `sl`（锚点） | `fresh` | run1 / run3 | **失败** | 2.69 / 2.97 | — | — | 无（被中断状态挡住） |

\* run3 期间宿主上并行跑了 4.3 GiB 镜像拷贝与 `e2fsck -fy`，I/O 竞争会抬高它的耗时；run3 只用于
「失败是否复现」，不用于绝对值。

三条可对外引用的结论：

1. **小包首次安装 13.12 s**（`sl`，含 `apt-get clean` 后的真实下载），与 PR #210 记录的
   `apt-get install sl` **14.9 s** 对齐（同一内核线，差 12%，在 run 间波动范围内）。
2. **`vim` 首次安装成功时 99.00 s**，其中 dpkg 解包 87 s、配置 2 s、apt 前后开销约 10 s。
3. **`vim` 首次安装 3 次里失败 2 次**：dpkg 在 `vim-runtime` 解包阶段调用 `fsync` 时收到 `E2BIG`
   （`Argument list too long`），内核同时打印 `ext4 journal transaction is too large`。失败后
   客户机的 dpkg 状态被中断，`python3`/`sl` 的后续事务全部 `rc=100`。

`reinstall` 行只能当下界：依赖已满足、不需要改依赖图，不等于「一台干净机器首次装 git/curl/python3
要等多久」。本镜像（`systemd-health` 的 m6 基座）预装了这三者，真正 fresh 的测量需要换一个不预装
它们的 rootfs，本轮没做（见 §6）。

## 2. 实验条件与命令

### 2.1 启动方式

```sh
tools/x86/debian/pkg_install_timings.sh \
  --kernel    target/prime/kernels/x86.elf \
  --initramfs target/x86-debian/m4/stage1/initramfs.cpio \
  --base-root target/systemd-health/daily.ext2 \
  --workspace target/t3b/runN \
  --accel kvm \
  --packages "git curl vim python3"
```

脚本复制一份基础镜像（不动原件），用 `debugfs` 注入一个 oneshot 探针 unit，然后经
`boot_debian.sh` 启动。QEMU 参数：`-enable-kvm -cpu host -smp 4 -m 4G`，
内核命令行 `console=hvc0 loglevel=4 init=/init -- --root-init=systemd --root-fs=ext4`。

文件与类型：

```text
$ file target/prime/kernels/x86.elf
target/prime/kernels/x86.elf: ELF 64-bit LSB executable, Intel i386, version 1 (SYSV),
statically linked, with debug_info, not stripped

$ file target/systemd-health/daily.ext2
target/systemd-health/daily.ext2: Linux rev 1.0 ext4 filesystem data,
UUID=e2c4f2cc-7d68-4d07-8f98-3f25fd04d610, volume name "ASTER_DEBIANE4"
```

客户机启动正常：Stage1 依次打印 `probe-block` → `root-found` → `handoff-done` → `exec`，随后
systemd 起服务，探针 unit 打印 `PKGT_BEGIN`。一次完整运行（拷贝镜像 + 启动 + 4 个 apt 事务）
约 2–3 分钟宿主墙钟。

### 2.2 计时方法

- 总耗时：客户机内 `date +%s.%N` 前后差，包住整条 `apt-get install`，下载计入。
- 每个事务前执行 `apt-get clean`，保证下载是用户真实要等的一部分。
- 解包/配置：从 `/var/log/dpkg.log` 的本次窗口里取
  `install|upgrade` → 最后一个 `status unpacked`（解包），
  `startup packages configure` → 最后一个 `status installed`（配置）。
  dpkg 时间戳只到秒，因此两项各带 ±1 s 误差。
- apt 固定开销 = 总耗时 − 解包 − 配置：`git` 10.3 s、`curl` 6.3 s、`vim` 10.0 s、`sl` 10.1 s、
  `python3` 11.0 s。`python3` 元包本体只有 28.2 kB，仍要等 13 s，大部分是 apt 重建缓存与
  `fsync` 的固定成本。

## 3. 每次运行的时间线

### 3.1 run1（02:46–02:47，KVM，宿主空闲）

```text
PKGT_BEGIN packages=git curl vim python3 anchor=sl install_timeout=900
PKGT_UPDATE_RC rc=1                     # 脚本里 rc=1 表示 update 成功（变量名是 update_ok）
PKGT pkg=git     pre=installed mode=reinstall rc=0   total=45.31 dpkg_unpack=na dpkg_configure=0
PKGT pkg=curl    pre=installed mode=reinstall rc=0   total=7.35  dpkg_unpack=na dpkg_configure=0
PKGT pkg=vim     pre=absent    mode=fresh     rc=100 total=22.10 dpkg_unpack=na dpkg_configure=na
PKGT pkg=python3 pre=installed mode=reinstall rc=100 total=2.59  dpkg_unpack=na dpkg_configure=na
PKGT pkg=sl      pre=absent    mode=fresh     rc=100 total=2.69  dpkg_unpack=na dpkg_configure=na
PKGT_DONE
```

这一轮的解包/配置列是 `na`，因为脚本当时的正则（`grep " unpack "`）匹配不到 dpkg.log 里的
`status unpacked`；`dpkg_configure=0` 也只是碰巧匹配到一行。事后用修好的规则从同一份
`dpkg.log` 重算：`git` 解包 35 s / 配置 0 s，`curl` 解包 1 s / 配置 0 s。§7 记录了这个修复。

### 3.2 run2（02:48–02:50，KVM，宿主空闲）

```text
PKGT pkg=sl  pre=absent mode=fresh rc=0 total=13.12 dpkg_unpack=2 dpkg_configure=1
PKGT pkg=vim pre=absent mode=fresh rc=0 total=99.00 dpkg_unpack=87 dpkg_configure=2
PKGT_DONE
```

`vim` 的 dpkg.log 窗口：`install vim-common` 18:48:54 → `startup packages configure` 18:50:2x，
配置阶段 18:50:21–18:50:23，最后 `status installed vim`。`e2fsck -fn` 对这份跑完的镜像干净。

### 3.3 run3（02:51–02:53，KVM，宿主并行做镜像拷贝与 e2fsck）

```text
PKGT pkg=git     ... total=50.91 dpkg_unpack=40 dpkg_configure=0
PKGT pkg=curl    ... total=7.74  dpkg_unpack=1  dpkg_configure=0
PKGT pkg=vim     ... rc=100 total=24.30 dpkg_unpack=16 dpkg_configure=na
PKGT pkg=python3 ... rc=100 total=3.04
PKGT pkg=sl      ... rc=100 total=2.97
PKGT_DONE
```

失败文本与 run1 逐字相同（见 §4.1），说明不是偶发的网络或超时。耗时受宿主 I/O 竞争影响，上一节
已标注。

### 3.4 run4（02:54，KVM，宿主空闲）

```text
PKGT pkg=python3 pre=installed mode=reinstall rc=0 total=13.01 dpkg_unpack=1 dpkg_configure=1
```

这一轮同时验证了 §7 的三处工具修复：驱动打印 `boot_debian: transcript ... satisfied "PKGT_DONE"`，
并把 `timings.tsv` 与 `guest/` 日志完整回写。

## 4. `vim` 失败证据链

### 4.1 客户机侧原文

run1 和 run3 的 `vim.apt.log` 结尾相同：

```text
Unpacking vim-runtime (2:9.1.1230-2) ...
dpkg: error processing archive /var/cache/apt/archives/vim-runtime_2%3a9.1.1230-2_all.deb (--unpack):
 unable to sync file '/usr/bin/vimtutor.dpkg-new': Argument list too long
Errors were encountered while processing:
 /var/cache/apt/archives/vim-runtime_2%3a9.1.1230-2_all.deb
```

`dpkg-query` 事后显示 `vim` 停在 `install ok unpacked`（run1），apt 返回 100。

### 4.2 内核侧原文

同一次运行的内核日志（客户机时间 90.982 s）：

```text
[    90.982] WARN  : ext2: ext2: metadata transaction failed: Error { errno: E2BIG,
msg: Some("ext4 journal transaction is too large") }
```

`E2BIG` 就是用户态的 `Argument list too long`：**dpkg 的 `fsync` 失败不是磁盘满，而是内核拒绝
提交一个过大的元数据事务**。

### 4.3 代码位置与上限

`kernel/src/fs/fs_impls/ext2/journal.rs:196-208`：

```rust
let tag_capacity =
    (BLOCK_SIZE - JBD2_SUPERBLOCK_HEADER_SIZE - JBD2_UUID_SIZE) / JBD2_TAG_SIZE;
if self.blocks.len() > tag_capacity {
    return_errno_with_message!(Errno::E2BIG, "ext4 journal transaction is too large");
}
```

常量是 `BLOCK_SIZE=4096`、`JBD2_SUPERBLOCK_HEADER_SIZE=12`、`JBD2_UUID_SIZE=16`、`JBD2_TAG_SIZE=8`，
所以**一个事务最多 508 个不同的元数据块**。当前实现把一个事务编码进**一个** JBD2 descriptor block；
真实 JBD2 允许一个事务跨多个 descriptor block，这里没有实现。超过 508 块就整笔拒绝。

触发路径是 `fs.rs:563 write_metadata_blocks`：它先把 `blocks` 一次性 `add_block` 进事务，再
`write_journal_transaction` → `encode()`。`vim-runtime` 有几千个小文件，dpkg 连续解包时累积的
inode 表、块位图、目录块很容易超过 508；某个文件的 `fsync` 触发提交时就整笔失败。

### 4.4 失败之后发生了什么

- 后续 `python3`、`sl` 事务都返回 `rc=100`，apt 输出停在
  `E: The following information from --solver 3.0 may provide additional context:`（dpkg 状态被中断）。
- 探针给 `python3`/`sl` 写的 `/opt/pkg-timings/*.apt.log` **在关机后的镜像里不存在**，
  而 `results.txt` 里两条 marker 都在：新文件的目录项没有落盘，运行时的写入停留在缓存里。
- `e2fsck -fn` 对 run1 镜像报告 `Inode 17741 has illegal block(s)` 等错误，`ncheck` 显示
  `17741 = /var/cache/apt/srcpkgcache.bin`。

**但这不等于文件系统被永久写坏。** `e2fsck -fn` 是只读检查，输出里明确写着
`Warning: skipping journal recovery because doing a read-only filesystem check`，它看到的是
未重放日志的陈旧状态。在副本上允许重放后再查：

```text
$ cp --sparse=always target/t3b/run1/pkg-timings.ext2 target/t3b/run1-fsck.ext2
$ e2fsck -fy target/t3b/run1-fsck.ext2
Free blocks count wrong ... Fix? yes
Inode bitmap differences:  -17741 ... Fix? yes
ASTER_DEBIANE4: ***** FILE SYSTEM WAS MODIFIED *****
$ e2fsck -fn target/t3b/run1-fsck.ext2
ASTER_DEBIANE4: 23216/262144 files (0.3% non-contiguous), 398627/1048576 blocks   # 干净
```

重放后 `srcpkgcache.bin` 不再存在（从未提交的孤儿 inode 被回收），文件系统干净。所以正确的说法是：
**一次 E2BIG 让这一轮安装的未提交元数据丢失、dpkg 状态中断，文件系统可以通过日志重放恢复**，
而不是「安装会写坏盘」。

## 5. 与历史数字对照

| 项 | 本轮 | 历史 | 说明 |
|---|---:|---:|---|
| `apt-get install sl`（首次） | **13.12 s** | 14.9 s | PR #210 `9b85b4d0a` 提交信息，同一内核线；本轮含 `apt-get clean` 后的真实下载，两者一致 |
| `vim` 首次安装 | **99.00 s**（成功样本） | 157.00 s | 2026-10-23 L1 popcon，`target/debian-popcon/console.txt:16945`；**早于 PR #210，且在 `kernel/core` 树上**，不能直接相减 |
| dpkg 配置阶段 | 0–2 s/包 | 22.4 s / 45 包 | 22.4 s 是 `dpkg --configure -a` 的整批基线，不是单包；本轮单包配置本来就接近 0 |
| `vim` 失败率 | 2/3（各 ~16–22 s 中断） | 无记录 | 2026-10-23 那轮 `vim install=ok`，当时的 ext2 日志实现不同（`kernel/core`） |

`git` 45.31 s、`curl` 7.35 s、`python3` 13.01 s 是 `reinstall` 下界，历史文档里没有同类可比值
（2026-10-23 那轮的 0.59/0.58/0.67 s 是「已安装」no-op 的 apt 快速返回，不是安装）。

## 6. 本轮覆盖不到的部分

1. **`git`/`curl`/`python3` 的首次安装**：m6 基座预装了三者，只能测 `--reinstall`。要测真 fresh，
   需要一个不预装它们的 Debian rootfs（或在镜像副本里安全移除依赖闭包），本轮没做。
2. **`python3` 元包 vs 运行时**：`python3` 是 28.2 kB 的元包，13.01 s 里只有 2 s 是 dpkg；
   用户真正关心的是 `python3.13` + 标准库闭包，应单独一行测。
3. **B 组（宿主 Linux 内核）**：未做。要可比必须同 QEMU 机型、同 4 vCPU/4 GiB、同镜像副本、
   同 `apt-get clean` 策略，只换 `-kernel`；不能拿宿主 `chroot` 冒充。
4. **失败率的统计强度**：3 次样本只够说明「会失败且可复现」，不足以给出精确失败率。
5. **E2BIG 的确定性触发条件**（多少个脏块、与 writeback 时序的关系）未做定向实验。

## 7. 本轮修掉的三处工具缺陷

这三处都不改内核代码，但会直接制造「看起来失败/看起来没数据」的假象，记录在此以免复现时再踩：

1. **阶段拆分的正则错**（`pkg_install_timings.sh`）：dpkg.log 没有 ` unpack ` 动作行，只有
   `status unpacked`；`startup packages configure` 行尾也没有空格。原实现因此永远报 `na`。
   已改为 `install|upgrade` → 最后一个 `status unpacked`，以及 `startup packages configure` →
   最后一个 `status installed`；用 run1/run2 的日志回归：git 35/0、curl 1/0、sl 2/1、vim 87/2。
2. **`boot_debian.sh` 的 `--wait-for` 假失败**：脚本开了 `set -o pipefail`，而匹配函数用
   `sed ... | grep -q`；`grep -q` 命中后立刻退出，sed 收到 SIGPIPE（141），于是整条管道判失败。
   run2/run3 都出现了「transcript 里明明有 `PKGT_DONE`，驱动却报 guest exited before」。
   已改为 `grep -c` 读完全部输入；run4 端到端验证通过
   （`boot_debian: transcript ... satisfied "PKGT_DONE"`）。
3. **探针 heredoc 里的反引号**：`pkg-timings.sh` 由未加引号的 heredoc 生成，注释里的反引号会被
   宿主当成命令替换执行（run3 的驱动日志里出现 `install: missing file operand`、
   `upgrade: command not found` 等）。生成出来的探针代码本身正确，不影响 run3 数据，但已把注释里的
   反引号去掉并加注释说明。

## 8. 复现命令

```sh
# 四包（git/curl/python3 为 reinstall，vim 为 fresh）
tools/x86/debian/pkg_install_timings.sh \
  --kernel target/prime/kernels/x86.elf \
  --initramfs target/x86-debian/m4/stage1/initramfs.cpio \
  --base-root target/systemd-health/daily.ext2 \
  --workspace target/t3b/runN --accel kvm \
  --packages "git curl vim python3"

# 对照锚点（fresh 小包）
tools/x86/debian/pkg_install_timings.sh ... --packages "sl" --anchor ""
```

产物在 `target/t3b/runN/`：`timings.tsv`、`transcript.txt`、`guest/<pkg>.{apt,dpkg}.log`、
跑完的镜像。本轮四份镜像与日志均保留在 `target/t3b/run1`…`run4`。

## 9. 一句话

x86 Debian 客户机在 KVM 下可正常启动和安装：小包首次安装 13 s 与历史 14.9 s 对齐，`vim` 首次安装
成功时 99 s；但内核 ext4 日志「单事务 ≤ 508 个元数据块」的限制让 `vim` 这类多文件包 3 次里有 2 次
在 16–22 s 处以 `E2BIG` 中断，并让同一次开机里的后续安装全部失败——这是当前最该修的用户可见问题。
