# mknodat 行为 vs gnulib 检测逻辑（L4 coreutils 卡点核查）

日期：2026-10-26
内核：主干（fork 停在 `c00db36af` 的代码线）　镜像：`ff-lag`（Debian trixie）

## 1. gnulib 侧到底检测什么

| 检测 | 位置 | 逻辑 | 失败表现 |
|---|---|---|---|
| `gl_FUNC_MKNOD` | gnulib `m4/mknod.m4`（已下载核对，serial 8） | 仅当 **非 root** 时运行：`mknod("conftest.fifo", S_IFIFO|0600, 0)` 必须成功；`geteuid()==0` 时程序**直接 `return 99`**，configure 随即 `AC_MSG_FAILURE("you should not run configure as root (set FORCE_UNSAFE_CONFIGURE=1)")` | configure 失败（**与 mknodat 实现无关**） |
| mknodat 存在性/语义 | gnulib 模块 `mkfifoat`，文档 `doc/posix-functions/mknodat.texi` | 需要 `mknodat` 存在；关注 S_IFIFO 支持、尾斜杠行为、已存在时应为 `EEXIST`（HP-UX 的 `EISDIR` 是 bug） | 编译/链接或 conftest 运行失败 |
| `GNULIB_TEST_MKNODAT` | coreutils `configure` 由 `gl_MODULE_INDICATOR([mknodat])` 生成 | 只是 **config.h 宏 / conftest 编译命令里的符号**，不是"测试名" | 出现在 DPKGB evidence 行里时容易被误读为"mknodat 测试失败" |

要点：**gnulib 没有独立的 `test-mknodat.c`**（gnulib 仓库只有 `tests/test-mknod.c` 与
`lib/mknodat.c`；coreutils 9.7 的 git 树里也没有 gnulib-tests）。`DPKGB … evidence=…GNULIB_TEST_MKNODAT_1…`
里的这个符号来自 configure 的 conftest 编译行，2026-10-25 的结论仍成立：那次失败是
**root 构建保护**，不是 mknodat 行为。

## 2. Asterinas 实测（本轮 guest 探针）

探针：`tools/x86/debian/mknodat_probe_check.sh`（root 与 uid=1000 两组）。

| 用例 | Linux 语义 | Asterinas 实测 | 判定 |
|---|---|---|---|
| `mknodat(AT_FDCWD,"fifo",S_IFIFO\|0600,0)` | 0 | **0** | ✓ |
| 重复创建同一 fifo | `EEXIST` | **EEXIST** | ✓ |
| 通过 dirfd 相对创建 fifo | 0 | **0** | ✓ |
| `S_IFREG\|0644` | 0，`stat` 为普通文件 0644 | **0，is_reg=1 mode=644** | ✓ |
| `mode=0644`（无类型位） | 按 `S_IFREG` | **创建为 REG** | ✓ |
| `S_IFDIR\|0755` | `EINVAL` | **EPERM** | ✗（不阻塞 gnulib） |
| `S_IFSOCK\|0600` | `EINVAL` | **rc=0，成功创建 socket inode** | ✗（不阻塞 gnulib） |
| `S_IFCHR\|0600`（root，dev 1:3） | 0，`rdev` 正确 | **0，is_chr=1 dev=1,3** | ✓ |
| `S_IFBLK\|0600`（root） | 0 | **0** | ✓ |
| 尾斜杠且目标不存在（`"fifo3/"`） | `ENOENT` | **ENOENT** | ✓ |
| `umask 027` 下 `0666` | 结果 0640 | **0640** | ✓ |
| dirfd 指向普通文件 | `ENOTDIR` | **ENOTDIR** | ✓ |
| 非 root（uid 1000）建 chr | `EPERM`（缺 CAP_MKNOD） | **EPERM** | ✓ |
| 非 root（uid 1000）建 fifo | 0（gnulib 的关键检查） | **0** | ✓ |

代码依据：`kernel/core/src/syscall/mknod.rs:17-66`
（`InodeType::from_raw_mode` → `File/CharDevice/BlockDevice/NamedPipe/Socket`，
`Socket` 走 `new_child`，其余类型 `_ => EPERM`）；
`kernel/core/src/fs/file/inode_attr/type.rs:38-48`（类型位 0 视为 `S_IFREG`，
非法类型 → `EINVAL`）；设备节点的权限检查在
`kernel/core/src/fs/vfs/path/dentry.rs:643,654-668`
（`Dentry::mknod` → `check_mknod_capability`，仅对 `CharDevice/BlockDevice`
要求 `CapSet::MKNOD`），因此非 root 建设备节点返回 `EPERM`，与 Linux 一致。

## 3. 差异清单与影响

| # | 差异 | Linux | Asterinas | 是否影响 gnulib / L4 |
|---|---|---|---|---|
| 1 | `S_IFSOCK` 被允许 | `EINVAL` | 创建成功 | 否（gnulib 不测；coreutils `tests/misc/mknod.sh` 可能测 invalid type，但 L4 只 `-b` 不跑 check） |
| 2 | `S_IFDIR` 返回 `EPERM` | `EINVAL` | `EPERM` | 否 |
| 3 | 设备节点权限检查 | 非 root `EPERM` | **同样 `EPERM`**（`dentry.rs:654` CAP_MKNOD） | 无差异 |
| 4 | root 构建保护 | configure 直接拒绝 | 同左（内核无关） | **是旧 L4 失败的真正原因**，已用 `FORCE_UNSAFE_CONFIGURE=1`（`tools/x86/debian/dpkg_build.sh:506-512`）绕过 |

## 4. 结论（截至本轮实测）

1. **gnulib 会检测的 mknod/mknodat 行为全部正确**：非 root 建 FIFO、`EEXIST`、
   尾斜杠 `ENOENT`、dirfd 相对路径、umask 生效——这些正是 `mknod.m4` 与
   `mkfifoat/mknodat` 模块关心的点。
2. 因此如果 L4 再次出现 `coreutils` 失败且 evidence 行里带
   `GNULIB_TEST_MKNODAT_1`，**优先怀疑不是 mknodat 行为**，而是：
   - `FORCE_UNSAFE_CONFIGURE=1` 没有进到 `debian/rules` 的子 make（环境未导出）；
   - 或 conftest 的编译/运行因其他 syscall/头文件失败。
   需要拿到 `pkg2` 的 configure 日志与实际 `$?` 才能定论。
3. 实际差异只剩两条（`S_IFSOCK` 被允许、`S_IFDIR` 返回 `EPERM`），都属于
   "与 Linux 对齐"的清理项，不构成本次 L4 卡点；如需修，作为独立小改动
   （`sys_mknodat` 对 `S_IFDIR/S_IFSOCK` 返回 `EINVAL`），**不要在 L4 归因里混入**。

## 5. 下一步取证清单

1. 复跑 L4 探针并保留 `pkg2` 的 `configure` 输出与 `DPKGB pkg=2 evidence=…` 原始行；
2. 若 evidence 指向 root guard：检查 `dpkg_build.sh` 里 `FORCE_UNSAFE_CONFIGURE`
   是 `env` 前缀还是导出变量（`debian/rules` 的 `make` 是否继承）；
3. 若指向 conftest 运行：抓该 conftest 的二进制与退出码（`config.log`）。
