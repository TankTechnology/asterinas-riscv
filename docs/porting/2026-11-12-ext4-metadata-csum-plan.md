# ext4 `metadata_csum` 实现方案（Phase 2b 未完成项）

## 现状

`tools/riscv/fs/ext4_feature_gate.py` 的第三个用例 `default-ext4`
（stock `mke2fs -t ext4`，默认开 `metadata_csum`）**故意断言挂载被拒绝**：
驱动里没有任何校验和代码（`grep -rn csum kernel/src/fs/fs_impls/ext2` 为空），
`super_block.rs` 的 allowed-features 掩码不含 `RO_COMPAT_METADATA_CSUM`。

要把它翻成 "pass"，需要让驱动在**每一处元数据写回**时重算 CRC32C，
否则 e2fsck 会把我们写过的每一个块都判成损坏。

## 校验范围（ext4 内核文档 `metadata_csum` 一节）

| 结构 | 位置 | 校验方式 |
| --- | --- | --- |
| 超级块 | `s_checksum` @ 0x3FC | `crc32c(seed, sb[0 .. 0x3FC])` |
| 块组描述符 | `bg_checksum` | `crc32c(seed, le32(bg), desc[0..0x1E] ‖ 0 ‖ desc[0x20..])` |
| 块位图 | `bg_block_bitmap_csum_lo/hi` | `crc32c(seed, bitmap, clusters_per_group / 8)` |
| inode 位图 | `bg_inode_bitmap_csum_lo/hi` | 同上，长度 `inodes_per_group / 8` |
| inode | `i_checksum_lo/hi` | `crc32c(seed, le32(ino), le32(i_generation), inode-with-zeroed-csum)` |
| 目录块 | 尾部 12 字节 `ext4_dir_entry_tail` | `crc32c(seed, le32(ino), le32(i_generation), block[0..bs-12])` |
| extent 块 | 尾部 4 字节 `ext4_extent_tail` | `crc32c(seed, block[0..bs-4])` |

`seed`：若 `metadata_csum_seed` 特性开启则取 `s_checksum_seed`；否则
`seed = crc32c(~0, s_uuid)`（16 字节 UUID）。

CRC 多项式是 **Castagnoli**（0x1EDC6F41，反射实现 0x82F63B78），
初始值 `~0`（由 seed 逻辑承担），结果**不取反**。

## 状态（2026-11-12）：已实现并通过门禁

`metadata_csum` 已在挂载时接受，`ext4_feature_gate.py` 三例全 PASS（含
`default-ext4` 的 `e2fsck -fn` 交叉校验），崩溃一致性门禁在 ext4 上开启校验和后
4/4 ok。

- `checksum.rs`：CRC32C + `fs_seed`/`superblock`/`group_desc`/`bitmap`/`inode`/`dir_block`
  六组校验函数（含参考向量 ktest）。
- 超级块：写回时按 `s_checksum`（0x3FC）重算（尾部 190 个 u32 中第 189 个）。
- 块组描述符：`write_group_descriptor` 写 `bg_block_bitmap_csum`/
  `bg_inode_bitmap_csum` 的 lo/hi 与 `bg_checksum`，支持 32/64 字节描述符。
- inode：`write_back_inode_desc` 读出整槽镜像、清零 `i_checksum_lo`(0x7C)/
  `i_checksum_hi`(0x82) 后重算并写回。

- 目录块尾：`DirBlockView::from_index` 预留 12 字节，`make_empty`/`add_entry`/
  `delete_entry`/`set_entry_target` 之后重写 `ext4_dir_entry_tail`。
- `metadata_csum_seed`（incompat bit 13）：seed 取 `s_checksum_seed`（0x270）。

**已修复的两个关键约定**（用真实 mke2fs 镜像对拍得出）：

1. 超级块校验和用的是 **`~0` 作为初值**，不是文件系统 seed；
2. inode 校验和只混入 **inode 号**，**不混入 generation**（generation 只用于
   目录块校验和）。

### 收尾时踩到的两个坑

1. **journal staging 只拷了 128 字节**：`fs.write_back_inode_desc` 在有日志时会把
   inode 块重新 stage 一遍，旧代码用 `raw_inode.as_bytes()` 覆盖前 128 字节，
   正好把 `i_checksum_lo`(0x7C) 冲成 0（`i_checksum_hi` 在尾部所以幸免）。
   改成 `group.inode_slot_image()` 返回**整槽镜像**并整槽拷贝。
2. **inode 校验和确实要混 generation**：用 `e2fsck -fy` 修正后的值反推才确定
   ——`crc32c(seed, ino) → crc32c(..., generation) → crc32c(..., inode bytes)`。
   generation 为 0 的 inode 上"混/不混"恰好相同，所以早期对拍被误导。

## 落地切分（纯内核侧，均可独立验证）

1. **crc32c 原语**：`kernel/src/fs/fs_impls/ext2/checksum.rs`，查表实现 +
   测试向量（`"123456789"` → `0xE3069283`）。无依赖，先落。
2. **超级块**：读入时若带 `metadata_csum` 就校验；写回时重算。
   gate 的 `default-ext4` 用例此时应能从"拒绝挂载"变成"挂载成功、只读访问 OK"。
3. **块组描述符**：`block_group.rs` 的 desc 写回路径重算；同时按
   `desc_size`（64 字节）读写，现有代码按 32 字节 staged 的部分要复核。
4. **位图**：`BlockGroup` 每次写位图块后重算对应两个 csum 字段。
5. **inode**：`inode write` 路径统一入口处重算（`i_extra_isize >= 4` 才写 hi）。
6. **目录块尾**：目录块分配/写满时插入 tail 并算 csum；`blocksize - 12`
   是可用空间上限。
7. **extent 块尾**：仅深度 > 0 的索引块需要。

## 验证门禁

- 单测/ktest：crc32c 向量；超级块/块组描述符/位图的 csum 函数对拍
  `e2fsprogs` 生成的真实镜像字段。
- 真机/QEMU 门禁：把 `ext4_feature_gate.py` 的 `default-ext4` 用例改为
  `expect: pass`，并在 probe 写入文件后跑 `e2fsck -fn`，要求 **0 修改**。
- `crash_consistency_gate.py` 的 ext4 特性列表加上 `metadata_csum`。

## 风险

- 块组描述符尺寸（32 vs 64 字节）与 `desc_size` 的读写必须一致，否则
  csum 会写进相邻字段；现有 `create_inode` 只 stage 前 128 字节的 inode，
  `i_extra_isize` 之外的尾部要显式清零（已由 `init_inode_extra` 处理）。
- `metadata_csum_seed` 与 `s_checksum_seed` 的启用顺序会影响所有旧镜像，
  必须先做 seed 抽象再做各结构。
- 目录块尾会挤占一个 `ext4_dir_entry_2` 的位置，长文件名路径要回归。
