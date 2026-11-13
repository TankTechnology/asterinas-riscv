# ext2 目录查找：先修扫描，再谈 HTree（2026-11-14）

## 结论先行

- 目录查找慢的主因**不是**没有 HTree，而是**逐条目读页缓存**：每个条目的
  header 和名字各一次页缓存读，约 1.2 µs/条目。
- 改成「每块读一次、在内存里扫」后（commit `216c0c93b`）：
  | 场景（638 条目目录，20000 次） | 改前 | 改后 |
  |---|---|---|
  | 首条目 | 426 µs | 76 µs |
  | 末条目 | 738 µs | 110 µs |
  | 不存在的名字（全扫） | 758 µs | 94 µs |
- 剩下的成本是**页缓存读本身**（约 15 µs / 4 KiB 块，接近 eMMC 延迟）。
  HTree 读路径同样要读这些块，因此**当前收益有限，暂不实现**，只在本文件
  记录设计与触发条件。

## 为什么不做 HTree（现在）

1. 查找已经降到 76–110 µs；HTree 最多再省掉「多扫几个块」的部分，
   而这块在 638 条目（6 个块）的目录里只有几十微秒的量级。
2. 真正的下一层瓶颈在 `PageCache::read_bytes`（15 µs/块）。HTree 的第一跳
   （dx_root）和叶块都会各付一次。先修页缓存读路径，HTree 才划算。
3. 收益面窄：dentry 缓存命中时根本不进 ext2；只有冷查找（首次遍历、
   包管理器的首轮探测）才会走到这里。

**满足以下任一条件时再实现**：页缓存读降到 ~2 µs/块；或出现单目录 >
50 个块的场景（约 5000+ 条目），此时线性扫描会成为可测瓶颈。

## 若要做：读路径设计（不改盘面格式）

目录置了 `INDEX_DIR` 标志时，块 0 是 `dx_root`：

```
[0..12)      "." 条目
[12..24)     ".." 条目
[24..)       fake dirent（ino=0, rec_len=blocksize-24）+ dx_root_info
             reserved_zero(4) hash_version(1) info_len(1) indirect_levels(1) unused_flags(1)
             之后是 dx_countlimit(limit u16, count u16) 与 dx_entry[]
dx_entry     hash(4) block(4)   // block 是相对目录文件的块号
indirect_levels>0 时，dx_entry.block 指向 dx_node 块（结构同上）
```

查找：`hash = dir_hash(superblock.seed, hash_version, name)`，
在 dx_entry 上二分找到 `hash` 的区间 `[e_i.block, e_{i+1}.block)`，
线性扫这些叶块；命中则返回，未命中则返回 ENOENT。

哈希版本（`s_def_hash_version`，与 dx_root 中的 `hash_version` 一致性检查）：
- 0 legacy：TEA 变体，种子取 superblock 的 `s_hash_seed[4]`
- 1 half_md4：MD4 的 3 轮 16 步变体，IV 固定，32 字节一块迭代
- 2 tea：少用，遇到直接回退

## 安全设计（实现时必须遵守）

1. **索引只用来缩小候选范围**：先扫候选叶块；找不到时**回退线性全扫**。
   这样即使哈希实现有误，也只会变慢，不会产生假 ENOENT。
2. 任何异常（魔数不符、level 过深、块号越界、hash_version 不支持）都回退。
3. 一个 `asterinas.ext2_htree_verify` 开关：开启时每次索引查找都再走一遍
   线性扫描并比对结果，不一致就 `warn!`。用一次全 `/usr` 遍历置零后再关掉。
4. 写路径不动：任何修改继续清 `INDEX_DIR`（现有 `mark_dir_modified` 行为）。

## 实测方法与数据来源

- 内核内基准：临时 proc 文件在写线程上下文里调用 `Inode::lookup(name)`
  20000 次（绕过 dentry 缓存），用 `syscall::profile::measure` 记录。
  测量代码已删除。
- 冷启动端到端：重启后立刻 `os.walk("/usr")`（41276 条目）8039 ms
  （改前同条件 9027 ms）。
