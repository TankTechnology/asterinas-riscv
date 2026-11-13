# ext2/ext4 驱动：功能完善与已知缺陷清单

> 本文中的 IPv4 地址均为 RFC 5737 文档保留地址。板上真实地址见本地配置。

日期：2026-11-14
代码：`kernel/src/fs/fs_impls/ext2/`
适用范围：驱动同时承担 ext2 与 ext4 镜像（同一份代码，按超级块特性位决定行为）

## 1. 现状：一次实测的映像特征

Megrez 根分区（Debian rootfs）的特性位实测为：

```
ext_attr resize_inode dir_index filetype sparse_super large_file
```

也就是说：**没有 journal、没有 extents、没有 metadata_csum** —— 名字叫 ext4，
实际是 ext2 + `dir_index`。驱动对这类卷走的是"无日志、直接写盘"的路径，
下面第 3 节的问题就出在这条路上。

## 2. 驱动当前支持的特性面

| 类 | 支持 | 只会被忽略/拒绝 |
| --- | --- | --- |
| compat | HAS_JOURNAL、EXT_ATTR、RESIZE_INO、DIR_INDEX、DIR_PREALLOC、IMAGIC | 其余 compat 位不参与校验（orphan_file、sparse_super2 等被静默忽略） |
| incompat | FILETYPE、RECOVER、EXTENTS、BIT64、FLEX_BG、CSUM_SEED | JOURNAL_DEV / META_BG / 任何未定义位 → `EINVAL` |
| ro_compat | SPARSE_SUPER、LARGE_FILE、BTREE_DIR、HUGE_FILE、DIR_NLINK、EXTRA_ISIZE、META_CSUM | 其余位（EA_INODE、QUOTA、BIGALLOC、VERITY…）→ `EINVAL` |

功能实现情况：

- **extents**：读写与截断均已实现（`inode/block_manager/extent.rs`）；
- **间接块**：直接/一级/二级/三级全部覆盖，截断路径有共享路径与错误重试用例；
- **HTree（dir_index）**：**只读兼容**。线性扫描能正确跳过索引根里的伪目录项，任何目录修改都会清掉 `INDEX_DIR` 标志退回线性目录（`dir/mod.rs:mark_dir_modified`）。功能正确，性能随目录尺寸线性下降；
- **JBD2**：内部日志的提交、checkpoint、mount 时 `RECOVER` 回放都有实现；外部日志明确拒绝（`EOPNOTSUPP`）；
- **xattr**：含 ea_inode（大属性存独立 inode）；
- **metadata_csum**：超级块/组描述符/位图/inode/目录尾全部维护（含 `metadata_csum_seed`）；
- **fallocate**：支持预分配模式；**不支持** punch hole。

## 3. 已修与待修的缺陷

### 3.1 已修：无日志卷的元数据写序（2026-11-13）

**症状**：板上反复出现

```
ext2: free_blocks: refusing to free already-free block 574686
<某文件>: Input/output error
```

**根因**：无日志卷上 inode table 与位图是两次独立的直接写盘，没有任何顺序约束。
分配方向（inode 先落盘、位图后落盘）一旦中断，磁盘上就留下"inode 引用了位图
标记空闲的块"。轻则后续释放报 EIO，重则该块被分配器再发给别的文件，造成两个
文件共享一个块——这正是 2026-11-12 根分区写满后整盘损坏的机制。

**修法**（`block_group.rs` + `fs.rs`）：同步时把位图写两遍。

1. 第一次：新分配立刻标占用，本轮释放的块仍然标占用（`write_pre_inode_bitmap`）；
2. 写 inode table；
3. 第二次：真正落下释放的位（`sync_metadata`）。

中间任何时刻崩溃，只可能留下"块泄漏"（`e2fsck` 直接回收），不可能留下悬空引用。
块位图与 inode 位图各自维护 `pending_frees`，两者都必须覆盖——只做块位图时，
新建 inode 的槽仍会先于 inode 位图落盘（门禁实测 `Inode bitmap differences: +12`）。

**验证**：`tools/riscv/fs/crash_consistency_gate.py --writer churn`

- 未修复内核：`dangling=[12]`，必失败；
- 修复后内核：`dangling=0`，通过；原 `--writer records` 门禁（ext2+ext4）无回归。

门禁用 `asterinas.ext2_sync_hold_ms` 把只有几微秒的窗口撑开，使"崩溃恰好落在
窗口内"变成确定性事件；不用钩子时该路径零开销。

### 3.2 待修：优先级 P1

| 缺陷 | 影响 | 方向 |
| --- | --- | --- |
| 无日志卷没有"脏"标记 | 崩溃后卷看起来仍是 clean，下一次挂载不会要求 fsck，损坏只能靠症状发现 | 挂载可写时置 `s_state` 非 clean，`sync`/卸载时置回 clean；非 clean 时至少打印告警并拒绝可写挂载，交给 fsck |
| HTree 只降级不实现 | 大目录（如 `/usr/share`）查找是线性扫描，`ls` 慢、`apt` 慢 | 读侧实现索引查找（dx_root/dx_node 解析），写侧保持"改了就降级"，或实现分裂 |
| 无 punch hole / `SEEK_HOLE` / `FIEMAP` | `fallocate -p`、`cp --sparse`、镜像工具、`fstrim` 都用不了；稀疏文件空洞无法表达 | 在 extent/间接块两层实现释放区间与空洞查询 |
| 崩溃后无孤儿处理 | 已 unlink 但未收尾的 inode 会泄漏；`orphan_file` 位被忽略 | 实现孤儿链表（`s_last_orphan`）或识别 `orphan_file`，挂载时回收 |

### 3.3 待修：优先级 P2

| 项 | 说明 |
| --- | --- |
| `EA_INODE` / `QUOTA` / `BIGALLOC` 拒绝信息 | 目前统一报 `EINVAL "unsupported ro-compatible feature"`，运维看不出到底缺哪个特性；应把缺失位打进错误消息 |
| 外部日志 | 明确拒绝即可，但错误应带 `journal_dev` 值，便于判断 |
| 目录尾 csum 与 `dir_nlink` 组合 | 已有实现，缺大目录高链接数的回归用例 |
| `FLEX_BG` 下的块组元数据定位 | 已实现，缺"组描述符指向其它组位图"的负向用例 |

## 4. 测试策略

1. **崩溃一致性**（已有）：`crash_consistency_gate.py`
   - `--writer churn`：无 fsync 的建/写/截断/删 + `syncfs`，崩溃后 `e2fsck -fn`
     不允许出现 `+` 差异（悬空引用），允许 `-` 差异（泄漏）；
   - `--writer records`：fsync 语义与数据完整性。
2. **特性矩阵**（已有）：`ext4_feature_gate.py`，覆盖 default ext4 / 纯 ext2 / 关闭
   checksum 等组合，并用 `e2fsck` 交叉校验。
3. **建议新增**：
   - `fsck` 期望矩阵：对每个 feature 组合生成"故意损坏"的镜像，断言内核给出
     可诊断的错误而不是静默接受；
   - 孤儿回收：构造 unlink 后崩溃的镜像，验证挂载时回收；
   - 大目录：10 万条目目录下 `lookup`/`create` 的延迟基线，作为 HTree 工作的验收。

## 5. 与既有工作的关系

- `metadata_csum`（2026-11-12）与本次写序修复是互补的：前者保证"写对了内容"，
  后者保证"写的顺序不会把崩溃放大成损坏"；
- 板端运维约束不变：任何硬重启后先 `e2fsck -fy`；日常重启用 `sync` + 干净重启。
