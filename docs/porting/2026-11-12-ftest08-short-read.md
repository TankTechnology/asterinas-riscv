# ftest08 短读：多写者并发下 readv 在文件中间提前结束

## 症状

LTP `fs` 套件唯一的能力失败项：

```
[FAIL] ftest08 exit=1
  Test[1]: xfr=1920 != 2048, zero read.
```

`ftest08` 用 16 个 128 字节的 iovec（共 2048 字节一个 chunk）在文件上做
lseek → readv → 校验 → writev 的循环，多个子进程（testers）交错写同一文件的
不同 chunk。读到"既不是 0 也不是一整块"的计数即判失败。

## 复现（已入库）

`tools/riscv/tests/ftest_readv_probe.c` + `target/probe/ftest/run.py`
（QEMU virt，initramfs 里直接跑探针）：

- 单进程、顺序写 4 个 chunk 再读回：**全对**（0/2048/2048…），稀疏洞读回
  2048 字节 0，文件被截短后的尾部读回 1920（这是合法的 EOF 短读）。
- 5 个进程交错写/读同一文件时**稳定复现**：

```
FTEST_PROBE mt me=2 off=157696 got=384  size=163968
FTEST_PROBE mt me=4 off=131072 got=1920 size=350208
```

`off + got < size`，即读在**文件中间**提前结束，不是 EOF。

## 已排除

- 探针在 page cache 拷贝处加了对比日志（`before/after = writer.avail()`），
  多写者复现时**从未触发**——每次 `page_cache.read` 都把 128 字节写满了。
- `readv` 的"部分成功后遇到错误就返回部分计数"分支也加了日志，同样未触发。

所以短读来自 `RamInode::read_at` 自己：它先用 `self.size()` 算出一个
`read_len`，再调用 page cache，并**无条件返回这个预先算出的长度**：

```rust
let (offset, read_len) = { let file_size = self.size(); ... };
page_cache.lock().read(offset, writer)?;
read_len
```

只要此刻的 `file_size` 快照比读者所在 chunk 小，就会返回 0/短计数；
而并发写者随后把文件写大，于是 `fstat` 看起来"数据明明在"。

## 修复

`RamInode::read_at` 现在按 inode 报告的大小把请求截断，并返回**实际拷贝**的
字节数：

```rust
let allowed = (file_size - offset).min(writer.avail());
writer.limit(allowed);
let before = writer.avail();
page_cache.lock().read(offset, writer)?;
before - writer.avail()
```

原来的写法把"读之前算出来的长度"直接返回，页面缓存实际少拷或文件大小快照
偏小时，`readv` 就会拿着一份与实际不符的计数回来——多写者并发时表现为文件中间
的短读。

## 复测

- 探针多写者场景连跑 3 次，**0 次短读**（修复前每次都能打出来）。
- `ltp_gate.py run --suite fs` 复跑结果见
  `target/ltp/results/<run>/summary.txt`（`fs_fill` 仍是 TCONF，环境性）。
