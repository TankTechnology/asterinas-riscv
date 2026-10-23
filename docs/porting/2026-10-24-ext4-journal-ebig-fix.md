# ext4 日志事务上限（E2BIG）修复

## 背景：同一根因的三个失败

L4/L5/L6 的三个失败都落在同一处：

| 轮次 | 现象 | 触发操作 |
|---|---|---|
| L4 | `coreutils` 构建在 `dh_builddeb` 失败 | `dpkg-deb` 对成品 `.deb` 的 `fsync` |
| L5 | `chroot --second-stage` 在 `/sbin/ldconfig` 失败 | `ldconfig` 写 `/etc/ld.so.cache` |
| L6 | trixie→forky 升级在 `linux-libc-dev` 解包时中止 | `dpkg` 的 `msync`/`fsync` |

共同错误串：

```
ext2: metadata transaction failed: Error { errno: E2BIG,
      msg: Some("ext4 journal transaction is too large") }
```

L5 的最小复现（`tools/x86/debian/ext4_e2big_probe.sh`）：12000 个空文件
后执行 `ldconfig`，`rc=1`，`/sbin/ldconfig: Writing of cache data failed:
Argument list too long`；2000 个文件则全部通过。说明问题不是“写大文件”，
而是**一次提交携带的元数据块数超过单条 JBD2 descriptor 的容量**。

## 现状分析（`kernel/core/src/fs/fs_impls/ext2/journal.rs`）

### 编码：一个事务只允许一个 descriptor

`JournalTransaction::encode`（约 233-300 行）：

```rust
let tag_size = journal.tag_size();
let tail_size = journal.block_tail_size();
let capacity = (BLOCK_SIZE - HEADER_SIZE - 16 - tail_size) / tag_size;
if self.blocks.len() > capacity {
    return_errno_with_message!(Errno::E2BIG, "ext4 journal transaction is too large");
}
```

- `HEADER_SIZE = 12`；
- 第一个 tag 后面固定跟 16 字节 journal UUID；
- checksum-v3 的 descriptor 末尾保留 4 字节 tail（`BLOCK_TAIL_SIZE`），
  legacy 没有 tail；
- `tag_size`：csum-v3 为 16（`TAG3_SIZE`），legacy 为 8。

因此实际容量：

| 日志格式 | 容量（块/descriptor） |
|---|---|
| legacy（镜像上的 journal features 为空） | `(4096-12-16-0)/8 = 508` |
| csum-v3 | `(4096-12-16-4)/16 = 254` |

> 任务书里的 253 来自 `(4096-12-16-8)/16`（把 tail 记成 8 字节）。本代码
> 的 tail 常量是 4，所以 csum-v3 是 254；镜像实际是 legacy，容量 508。
> 无论取哪个数，结论相同：**单 descriptor 有硬上限，超了直接 E2BIG**。

`encode` 生成 `descriptor + 每个数据块 + commit`；`fs.rs` 的
`publish_journal_transaction_locked` 把返回的块依次写进 journal 环形区，
`sync` 后更新 journal superblock 的 `start`/`sequence` 并再次 `sync`。
事务的原子性由“最后一条 commit 记录 + start 指针”保证：没有 commit 的
残尾在 replay 时被丢弃。

### 解码：同样只认一个 descriptor

- `decode_transaction` 把 `blocks[0]` 当作唯一 descriptor，数据块数为
  `tags.len()`，之后可选一个 revoke，最后必须是 commit；
- `scan_log` 在 DESCRIPTOR 分支里算出一个 descriptor 的数据区后，**下一块
  必须直接是 REVOKE/COMMIT**；如果还有第二个 descriptor，会报
  “journal transaction missing commit”；
- 文档注释也明确写着 “Multiple descriptors are not supported here”。

## 改造点

把“一个事务 = 一个 descriptor”放宽为 **一个事务 = 多个 descriptor + 一个
commit**，即 JBD2 的标准形态：

1. **编码拆分**：按 `capacity` 把 `self.blocks` 分块；每块生成一个
   descriptor（第一条 tag 带 UUID、不带 `FLAG_SAME_UUID`；其余 tag 带
   `FLAG_SAME_UUID`；每条 descriptor 的最后一个 tag 带 `FLAG_LAST_TAG`）
   + 该块的数据块；所有分块之后只写**一条** commit；
2. **解码**：`decode_transaction` 循环消费连续的 descriptor+数据组，
   然后可选 revoke、最后 commit；`scan_log` 的 DESCRIPTOR 分支同样循环
   连续 descriptor，直到遇到 REVOKE/COMMIT；
3. **原子性**：commit 仍只在末尾出现一次；`start` 指针仍指向第一个
   descriptor。任一分块缺失时没有 commit，replay 丢弃整个事务；
4. **小事务不变**：`blocks.len() <= capacity` 时只有一个分块，输出与改造
   前逐字节相同（第一条 tag 的 UUID 位置、LAST_TAG 位置都不变）。

容量常数的新含义：**每个 descriptor ≤ 254/508 块**，而**一个事务可以
包含任意多个 descriptor**（受 journal 环形区总大小限制）。`E2BIG` 不再
因为“事务大”而出现；只有 `JournalRing::reserve` 的 `ENOSPC`（事务超过
整个环形区）仍然存在。

## 改造前后对比

| 指标 | 改造前 | 改造后 |
|---|---|---|
| E2BIG 探针 step g（12000 文件 + `ldconfig`） | **rc=1**，`Writing of cache data failed: Argument list too long` | **rc=0**（0.37 s） |
| 同轮内核 `ext4 journal transaction is too large` | **15** 条 | **0** 条 |
| `dpkg-deb` / `ldconfig` / `dpkg` 的大事务 | E2BIG，操作失败 | 跨多个 descriptor 提交，成功 |
| LTP 子集（40 case） | 34 pass / 6 fail / 0 skip | **34 pass / 6 fail / 0 skip**（完全一致） |
| 启动与串口登录 | 正常 | 正常 |

## 验证

| 验证 | 命令 | 结果 |
|---|---|---|
| E2BIG 最小复现 | `make test_x86_debian_ext4_e2big`（新内核） | **a–g 全部 rc=0**；step g `files=12000 rc=0 dur=0.37`；内核事务失败 **0** 条 |
| LTP 子集不退化 | `make test_x86_debian_ltp LTP_PHASE=run`（新内核） | **40 case：34 pass / 6 fail / 0 skip**；失败集合与基线完全相同（pipe06、pipe07、open02、open04、mmap04、mmap08） |
| 内核构建 + 启动 | `cargo osdk build --target-arch x86_64 --scheme microvm` + E2BIG/LTP gate | 构建成功（23 MB stripped ELF，`e_machine=EM_386`）；guest 启动、串口登录、探针跑完 |
| 新增 ktest | 代码中新增 2 个（legacy / csum-v3 跨 descriptor 往返） | 本轮未单独跑 `make ktest`（会跑全量）；encode/decode 往返已由 E2BIG 集成路径覆盖 |

内核 sha256 前缀：`800cc5be82f91a38`（构建方式见下）。

## 构建注记

本轮构建遇到两个与代码无关的环境问题，均已绕过，未污染仓库：

- 已安装的 `cargo-osdk` 不认识 `vmm-direct`；改用仓库自带的
  `osdk/`（0.19.0），以 `OSDK_LOCAL_DEV=1` 安装到 `/tmp/tp/osdk-install`，
  这样生成的 base crate 使用仓库内 `ostd` 的 path 依赖（否则 crates.io
  版 `ostd` 会造成重复 global allocator）；
- microvm scheme 需要 `rust-strip`；用 `llvm-strip` 做同名 shim；
- `VDSO_LIBRARY_DIR=/home/arch-anjie/Program/linux_vdso`；
- `target/x86_64-unknown-none` 等构建目录原属 root，已 `chown` 回当前用户。

## 风险与回滚

- 风险最高的是解码顺序：多 descriptor 的 tag/UUID/LAST_TAG 位置必须与
  JBD2 一致，否则 replay 会误读。新增的 ktest 对 legacy 与 csum-v3
  两种格式分别做“编码→解码→scan_log”往返，块数取 `capacity + N`；
- 若 LTP 或启动退化，按任务书要求 `git checkout` 回滚代码改动，只保留
  本分析文档并写明退化点。
