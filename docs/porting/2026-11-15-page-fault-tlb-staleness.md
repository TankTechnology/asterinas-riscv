# 每个匿名页缺两次页：EIC7700 缓存了无效翻译（2026-11-15）

## 1. 现象

桌面卡，拆开看：Firefox 滚动时单核 100%，其中 **40% 是内核态**（3.07 s 里 1.25 s），
期间 7 千多次缺页。把缺页单独量出来（`/tmp/faultbench*.c`，128/64 MiB 分区逐页触摸）：

| 场景 | 缺页次数 | 每次 |
| --- | ---: | ---: |
| 匿名页，写一次 | **2.00 次/页** | 8–10 µs |
| 匿名页，只读一次 | **2.00 次/页** | 8.1 µs |
| 文件页，只读 | 0.08 次/页 | 一次铺 12 页（fault-around 生效） |

Linux 上这两种匿名场景都是 1 次。**同一份内核在 QEMU 里也是 1 次**，只有 EIC7700 上是
2 次——这说明它不是通用的软件逻辑问题，而是这台机器的 MMU 行为。

进一步用单页探针把两次拆开：

```
first read  : 2 faults
second read : 0 faults
first write : 1 faults      ← 这一页此前已被读缺页映射成只读（零页 COW）
second write: 0 faults
```

## 2. 根因

缺页处理器在 `VmQueriedItem::None` 分支里 `cursor.map(...)` 装好新页表项就返回用户态，
**没有做任何 TLB 失效**。RISC-V 手册只保证"写了页表之后，`sfence.vma` 让改动可见"，
并没有禁止核把 **V=0 的无效项**也缓存起来；EIC7700（P550）就是这么做的。于是：

1. 第一次访问 → MMU 走页表，拿到"无效"，把它缓存下来 → 缺页；
2. 处理器装好页表项，但不 flush → 返回；
3. 指令重试 → MMU 命中缓存里的"无效" → **再缺一次**；
4. 第二次缺页走的是"已映射"快路径 `record_page_access`，那里**会**发 flush（issue +
   dispatch + sync），这才让新页表项生效；
5. 第三次访问才成功。

QEMU 的 TCG 不缓存无效翻译，所以同一份内核在仿真里只缺一次——这正是把两边的数字放
在一起才看出问题的地方。

COW 分支本来就没有这个问题：它先 `unmap`（内部会发 flush）再 `map`。

## 3. 修复

在冷装页的路径上，装完页表项后对**该页**发一次 TLB 失效（`issue` → `dispatch` →
`sync`，与 `record_page_access` 和 COW 分支同一套动作）：

```rust
// vm_mapping.rs, handle_single_page_fault, the `VmQueriedItem::None` arm
let before = Translation::Absent;
let after = if page_flags.contains(PageFlags::W) {
    Translation::Writable
} else {
    Translation::ReadOnly
};
if flush_required(before, after) {
    cursor.flusher().issue_tlb_flush(TlbFlushOp::for_range(addr..addr + PAGE_SIZE));
    cursor.flusher().dispatch_tlb_flush();
    cursor.flusher().sync_tlb_flush();
}
```

成本上这是**严格更便宜**的：现在因为第二次缺页绕道 `record_page_access`，本来就已经
每页发了一次同样的 flush，外加一次陷入、一次查找、一次 `protect_next`。修复后少一次
陷入，flush 次数不变。

## 4. 形式化验证：`kernel/libs/tlb-staleness`

仓库里对 CoW 决策已经有先例（`kernel/libs/cow-decision`，`cargo kani` 证明"只有引用
计数为 1 时才原地升级"）。这次把"什么时候必须 flush"也做成同样形状的可判定函数：

```rust
pub enum Translation { Absent, ReadOnly, Writable }
pub fn flush_required(before: Translation, after: Translation) -> bool { before != after }
pub fn served(cached: Option<Translation>, entry: Translation, access: Access) -> bool
```

MMU 被建模成"一个地址上的一条缓存条目"：有缓存就按缓存服务，没有就走页表并把结果
（**包括无效结果**）缓存下来。内核真的调用 `flush_required` 来做决定，所以证明对象就是
实现，不是一份并行文档。

**Kani（`cargo kani -p tlb-staleness`）3 个 harness，0 失败**：

| harness | 结论 |
| --- | --- |
| `sufficiency_of_the_flush` | 页表项变化且 flush 后，重试的访问一定被服务（双缺页不可能发生） |
| `necessity_of_the_flush` | 页表项变化但不 flush 时，`served(Some(before), after, access) == false`，即重试必然再缺页——板子上观察到的行为就是这条反例 |
| `flush_required_is_exact` | 规则既不漏也不多：只有真的改变页表项才要求 flush |

另有 4 个不带验证器的穷举测试（`cargo test -p tlb-staleness`），遍历全部
`{Absent, ReadOnly, Writable} × {Read, Write}` 组合，其中两条直接复现"不 flush 就双缺页"。

## 5. 仿真验证

| 项 | 结果 |
| --- | --- |
| `cargo osdk test`（ktest，QEMU） | 432 passed / 0 failed |
| 缺页探针（QEMU） | 仍 1.00 次/页，无回归、无 panic |
| `preflight_kernel.sh` | 结构检查 + 冒烟通过 |

仿真**不能**证明性能变好——它本来就不复现这个 bug；它证明的是改动没有破坏别的东西。

## 6. 真机验证

部署走 `preflight` → `deploy` → 重启（回滚镜像 `/root/kernel-rel.img.bak` 在位），
两次部署都 14–15 s 回来，桌面 / VNC / Firefox 全部正常，无 failed unit、无 panic。

### 6.1 缺页次数（确定性指标）

```
修复前: first read 2 faults / first write 1 / …        bench3 1.99–2.00 次/页
修复后: first read 1 fault  / first write 1 / …        bench3 1.00 次/页
```

`fault5` 的语义也对上了：修复后"读一次"只缺一次页，之后的读不再缺；写仍然只有一次
COW 缺页。

### 6.2 端到端耗时（同一台板、滚动回滚做 A/B）

| 负载 | 修复前 | 修复后 | 倍数 |
| --- | ---: | ---: | ---: |
| 单线程连续触摸 256 MiB（5 轮取最好） | 2461.7 ms（2.00 次/页） | 1046.1–1516.8 ms（1.00 次/页） | **1.6–2.35×** |
| 4 线程各触摸 64 MiB（3 轮取最好） | 1872.6 ms（1.99 次/页） | 1421.5 ms（1.00 次/页） | **1.32×** |

多线程的收益小于单线程，因为 flush 要等其它 CPU 的 ACK（`target_cpus` 包含所有跑过
这个地址空间的核）。单线程时集合通常只有当前核，不产生 IPI。

> 测量方法：把修复前的内核留在 `kernel-rel.img.bak`，第一次部署修复版后 `cp` 回去重启
> 测一轮，再部署修复版测一轮。同一份静态二进制、同一目录、连续执行。

### 6.3 对桌面的意义

缺页从"每页两次陷入 + 一次 flush"变成"一次陷入 + 一次 flush"，内核态的那部分开销
按缺页次数比例下降。浏览器滚动里内核态曾占 40%，这条路径是其中最大的一块；剩下的
是纯用户态的软件渲染（见 `2026-11-15-desktop-latency.md` 的分析）。

## 7. 遗留

- 多线程下每次缺页仍要给其它核发 IPI 并等 ACK。可以改成只 flush 本核：其它核若缓存了
  无效项，会自己再缺一次页并在那里 flush（当前的"bug 路径"正好就是它的自愈路径）。
  代价是正确性论证从"全局"退化成"每核"，收益约 3–4 µs/页（多线程场景 17%）。暂不做。
- 匿名映射仍然没有 fault-around（文件映射有，一次铺 12 页）。这是下一个数量级。
