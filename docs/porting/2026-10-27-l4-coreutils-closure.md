# L4 coreutils 收口：root 守卫复现、当前内核重跑、mknod FIFO 权限链审计

日期：2026-10-27
内核：`86cf3f5d0`（`target/osdk/asterinas-osdk-bin.elf`，sha256 前 32 位 `8c71177bd24c060db7eb4fc57443e241`）
基础镜像：`target/systemd-health/daily.ext2`（与 2026-10-25 的 3/3 基线同一镜像）
本轮：只跑探针与只读审计，**内核代码零改动**，未 push

## 0. 结论

1. **"2/3" 的失败点不是内核**：coreutils 9.7 的 `configure` 在 guest 内以 root 运行，
   gnulib `mknod.m4` 的检测程序在 `geteuid()==0` 时**先返回 99、根本不调用 `mknod()`**，
   configure 随即 `AC_MSG_FAILURE`。同一程序在 Linux 上以 root 运行得到完全相同的结果。
2. **我们的 mknod FIFO 语义没有 root 要求**：`sys_mknodat` → 父目录 `MAY_WRITE|MAY_EXEC`
   → `check_mknod_capability` 只对字符/块设备要求 `CAP_MKNOD`，`NamedPipe` 直接放行。
   非 root 建 FIFO 在 Asterinas 上成功（本轮实测）。
3. **当前内核重跑**：单包 coreutils `result=ok`（build 417.51 s，1 次尝试，两个 `.deb`），
   构建产物在 guest 内安装后 `ls/cp/mv/du/df` 全部正常；随后**三包全量单次运行**
   `hello / coreutils / python3-minimal` 全部 ok（**L4 = 3/3**，23 个 `.deb`，panic=0）。
   L4 状态页自 2026-10-25 起已是 3/3（root 守卫由 `91896fad6` 的
   `FORCE_UNSAFE_CONFIGURE=1` 解决），本轮是在 `86cf3f5d0` 上的**回归确认**，
   不是新的修复。

## 1. 任务 A：复现基线与当前内核重跑

### 1.1 "2/3" 失败原文（2026-10-25 基线日志）

`docs/porting/evidence/2026-10-25-l4-mknodat/configure-root-guard.txt`：

```
checking for mknodat... yes
checking for mkfifoat... yes
checking whether mknod can create fifo without root privileges... configure: error: \
  you should not run configure as root
  (set FORCE_UNSAFE_CONFIGURE=1 in environment to bypass this check)
configure:65580: ./conftest
configure:65580: $? = 99
```

`$? = 99` 是 root 守卫的返回值，`mknod()` 从未被执行。

### 1.2 当前内核重跑 coreutils（`SOURCE_BUILD_PACKAGES=coreutils`）

```
DPKGB pkg=1 start name=coreutils
DPKGB pkg=1 disk_free_kb=2084824 attempt=1
DPKGB pkg=1 stage=builddeps rc=0 dur=295.18 attempt=1
DPKGB pkg=1 stage=source   rc=0 dur=10.77  attempt=1
DPKGB pkg=1 stage=build    rc=0 dur=417.51 attempt=1
DPKGB pkg=1 artifact=coreutils-dbgsym_9.7-3_amd64.deb
DPKGB pkg=1 artifact=coreutils_9.7-3_amd64.deb
DPKGB pkg=1 done name=coreutils result=ok attempts=1 dur=733.70
DPKGB summary total=1 ok=1 fail=0
```

`gate_rc=0`、`panic=0`。build 阶段 417.51 s，比 2026-10-25 的 575.99 s 快（同一镜像、同一内核特性集）。

guest 内 `pkg1-build.log`（当前内核）第 822 行：

```
checking whether mknod can create fifo without root privileges... no
```

随后 `1137:config.status: creating Makefile`、`7202/7203:dpkg-deb: building package …`，
`dpkg-buildpackage: info: binary-only upload (no source included)` —— 与 2026-10-25 基线逐行一致。

### 1.3 guest 内验收（同一镜像，替换 daily checklist 跑自定义检查）

```
L4S conftest_build=ok
L4S conftest_root rc=99 euid=0
L4S conftest_user uid=1000 rc=0
L4S fifo_stat prw------- 1 asterinas asterinas 0 Oct 26 16:25 conftest.fifo
L4S source=ok Fetched 6189 kB in 6s
L4S configure_rc=1
L4S cfg 791:checking whether mknod can create fifo without root privileges... configure: error: in '/tmp/coreutils-9.7':
L4S cfg 792-configure: error: you should not run configure as root (set FORCE_UNSAFE_CONFIGURE=1 in environment to bypass this check)
L4S cfg 793-See 'config.log' for more details
L4S deb=3024912 /var/tmp/dpkg-artifacts/coreutils_9.7-3_amd64.deb
L4S dpkg=ok Processing triggers for man-db (2.13.1-1) ...
L4S ls_version=ls (GNU coreutils) 9.7
L4S owned=coreutils: /usr/bin/ls
L4S ls_rc=0 ls_lines=2 a.txt,b.txt,
L4S cp_rc=0 err=
L4S mv_rc=0 old_gone=yes new_present=yes err=
L4S du 4096	/tmp/l4s/big.bin
L4S df tmpfs            2084824     0   2084824   0% /tmp
```

判读：

- **"2/3" 基线在当前内核上完整复现**：root 下 conftest `rc=99`，真实 `./configure`
  `rc=1` 并停在 mknod 这一行（与 2026-10-25 的日志同文同位置）。
- **非 root 建 FIFO 正常**：uid 1000 的 conftest `rc=0`，`stat` 得到
  `prw------- 1 asterinas asterinas`（S_IFIFO + 0600），**内核侧没有任何 root 要求**。
- **产物可用**：本轮构建的 `.deb` 安装成功（`dpkg -i` 走完 man-db trigger），
  `ls (GNU coreutils) 9.7` 归属 `coreutils`；`ls/cp/mv` 语义正确，`du` 对 4 MiB 文件报
  4096 KiB，`df` 正常输出 tmpfs 行。

注：这次 guest 检查替换了 `daily_use_checklist.sh`，所以 gate 最后以
`FAIL (no result for: DESKTOP, APP, APT, …)` 退出 1——这是**替换了清单**的预期结果，
不是内核失败；本轮没有内核改动，不需要 7 域矩阵。

### 1.4 当前内核三包全量（直接判 3/3，不是账目合并）

```
pkg  name              result  attempts  dur      builddeps  source  build    artifacts
1    hello             ok      1         274.31   183.40     5.04    84.44    2
2    coreutils         ok      1         572.36   125.35     10.10   424.44   2
3    python3-minimal   ok      1         370.86   334.52     5.18    27.18    19

DPKGB summary total=3 ok=3 fail=0   gate_rc=0   panic=0
```

（`python3-minimal` 的源包是 `python3-defaults` 元包，构建阶段只有 27.18 s；
总时长 370.86 s 与 2026-10-25 的 369.11 s 一致。）

**判据达成：L4 = 3/3，且是当前内核 `86cf3f5d0` 单次运行的结果。**

## 2. 任务 B：gnulib 期望 vs 我们的实现（只读）

### 2.1 gnulib 到底测什么

`m4/mknod.m4`（serial 8，本地副本 `/tmp/mknod.m4`）：

```c
/* Indeterminate for super-user, assume no.  Why are you running
         configure as root, anyway?  */
      if (geteuid () == ROOT_UID) return 99;          /* :39 */
      if (mknod ("conftest.fifo", S_IFIFO | 0600, 0)) return 2;   /* :40 */
```

```m4
[gl_cv_func_mknod_works=yes],
[if test $? = 99 && test x"$FORCE_UNSAFE_CONFIGURE" = x; then   /* :42 */
   AC_MSG_FAILURE([you should not run configure as root ...])
 fi
 gl_cv_func_mknod_works=no]
```

- **期望**：非 root 时 `mknod("…", S_IFIFO|0600, 0)` 返回 0（BSD 上有些系统要求 root，
  gnulib 为此提供 `rpl_mknod` 替换）。
- **实际**：guest 里构建以 **euid 0** 运行，程序直接 `return 99`；
  设了 `FORCE_UNSAFE_CONFIGURE=1` 后 configure 不中止，但缓存值仍是 `no`
  （因为 99 ≠ 0）——**这与在 Linux 上以 root 构建完全相同**。
- 宿主机实测（同一 conftest，ext4）：uid 1000 → `rc=0`；`unshare -r`（euid 0）→ `rc=99`
  （`evidence/2026-10-27-l4-coreutils/gnulib-conftest-linux.txt`）。

### 2.2 我们的 mknod FIFO 权限链（文件:行号）

| 步骤 | 位置 | 行为 |
|---|---|---|
| syscall 入口 | `kernel/core/src/syscall/arch/x86.rs:312`（`SYS_MKNOD=133`）、`:377`（`SYS_MKNODAT=259`） | 两个入口；`sys_mknod` 委托 `sys_mknodat(AT_FDCWD,…)`（`syscall/mknod.rs:68-75`） |
| 类型解析 | `syscall/mknod.rs:30` → `fs/file/inode_attr/type.rs:38-48` | `S_IFIFO = 0o010000` → `InodeType::NamedPipe`（`type.rs:11`） |
| 父目录权限（**唯一的常规权限判定**） | `fs/vfs/path/mod.rs:787-793` → `:331-335` | `check_dir_entry_mutation`：`check_mount_writable` + 目录 inode 的 `MAY_WRITE\|MAY_EXEC`。**普通 POSIX 权限，不涉及 root/CAP** |
| 能力检查 | `fs/vfs/path/dentry.rs:636` → `:647-662` | `check_mknod_capability`：`matches!(CharDevice|BlockDevice)` 才继续；**`NamedPipe` 在第 649 行直接 `return Ok(())`** |
| 设备节点的 root 要求 | `dentry.rs:657-661` → `security/lsm/hooks/capability.rs:10` → `security/lsm/modules/capability.rs:28-49` | 只有字符/块设备需要 effective `CapSet::MKNOD`（`process/credentials/capabilities.rs:38`，`1<<27`）；缺能力 → `EPERM`。**FIFO 不走这里** |
| 具体创建 | ext2：`fs/fs_impls/ext2/impl_for_vfs/inode.rs:196-215`；ramfs：`fs/fs_impls/ramfs/fs.rs:966-1005` | `MknodType::NamedPipe` → 建 `InodeType::NamedPipe` inode，无额外权限检查 |

完整摘录见 `evidence/2026-10-27-l4-coreutils/mknod-permission-chain.txt`。

**结论**：我们对 FIFO 创建**没有 root 要求**，与 Linux 语义一致；
gnulib 的 "no" 来自 euid 判定，不是内核行为差异。**无需修内核**。

### 2.3 已知但与本题无关的语义差异（沿用 2026-10-26 实测，本轮未复测）

| 差异 | Linux | Asterinas | 影响 gnulib/L4 |
|---|---|---|---|
| `S_IFSOCK` | `EINVAL` | 成功创建 socket inode（`mknod.rs:59-61`） | 否 |
| `S_IFDIR` | `EINVAL` | `EPERM`（`mknod.rs:62`） | 否 |

建议（不在本轮执行）：让 `sys_mknodat` 对 `S_IFDIR` / `S_IFSOCK` 按 Linux 返回 `EINVAL`，
作为独立小改动，不混入 L4 归因。

## 3. 任务 C：状态页

`docs/porting/debian-source-build.md` 的 L4 状态在 2026-10-25 就已经是 **3/3** ✅，
所以本轮不是"从 2/3 改到 3/3"，而是**补一条当前内核的回归确认**：

> 回归确认（2026-10-27，内核 `86cf3f5d0`）：三包全量单次运行 3 ok / 0 fail，
> 共 23 个 `.deb`，panic=0（hello 274.31 s、coreutils 572.36 s、python3-minimal 370.86 s）。

同时保留了"2026-10-24 = 2 ok / 1 fail"的历史基线，避免读者把旧基线当成现状。

## 4. 证据

- `evidence/2026-10-27-l4-coreutils/`：
  - `gnulib-conftest-linux.txt`（m4 原文 + 宿主机 rc 0 / 99）
  - `mknod-permission-chain.txt`（文件:行号摘录，198 行）
  - `console-dpkgb.txt`（当前内核 coreutils 构建的 DPKGB 行）
  - `configure-current-kernel.txt`（guest `pkg1-build.log` 的 configure/log 行号）
  - `guest-sanity.txt`（guest 内 conftest / configure / .deb / ls/cp/mv/du/df 原始行）
- 主机工作区：`target/l4-close/`（coreutils 单包与三包全量两次运行）、
  串口全量 `console.txt`；guest 日志 `/var/tmp/dpkg-logs/`、产物 `/var/tmp/dpkg-artifacts/`
