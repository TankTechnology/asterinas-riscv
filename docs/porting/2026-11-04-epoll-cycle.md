# epoll：环引用被接受并导致 `epoll_wait` 挂死

日期：2026-11-04

## 问题

`EpollFile::add_interest` 只检查两件事：

- 被添加的 epoll 集合是否**就是自己**（`EPOLL_CTL_ADD` 自己 → `ELOOP`）；
- 嵌套深度是否超限（`epoll_ctl04` 的五层链）。

它没有检查"被添加的集合是否已经（直接或间接）监视着当前集合"，所以两个
epoll 集合可以互相监视：

```c
epoll_ctl(a, EPOLL_CTL_ADD, b, ...);   /* a 监视 b */
epoll_ctl(b, EPOLL_CTL_ADD, a, ...);   /* b 监视 a：Linux 返回 ELOOP，本内核返回 0 */
```

## 复现（板子，旧内核）

`tools/riscv/perf/epoll_cycle_check.c`，静态编译后在板上运行：

```text
EPCYCLE add(a<-b)=0 errno=0
EPCYCLE add(b<-a)=0 errno=0 (ELOOP=40)     ← Linux 应为 -1/ELOOP
```

第二次 `EPOLL_CTL_ADD` 返回 0，环成立。随后给 `a` 加一个可读的管道并写入，
`epoll_wait(a, ..., 200ms)` **永不返回**——带超时也挂死，不符合 epoll 语义。
这也是 LTP `epoll_ctl05` 存在的原因（该用例此前在清单里被注释掉，从未运行）。

## 修复

`kernel/src/events/epoll/file.rs`：

1. `add_interest` 在插入前，若目标是一个 epoll 集合，先调用
   `child.reaches(self)` 做**可达性检查**（DFS 沿对方的 interest 条目走，
   用 visited 集合防环、深度有限），可达则返回 `ELOOP`。
2. 该 DFS 需要同时持有两个集合的 interest 锁，因此新增全局 `LOOP_CHECK`
   互斥量把"结构遍历"串行化——等价于 Linux 的 `epmutex`，避免两个方向相反的
   `EPOLL_CTL_ADD` 互相等锁。

DFS 遍历时先收集弱引用并释放被遍历集合的锁再递归，不会持有锁递归。

## 验证

- QEMU（4 核）epoll 子集 **10/10**：`epoll_create01/02`、`epoll_create1_01/02`、
  `epoll01`、`epoll_ctl01`、`epoll_ctl03`、`epoll_ctl04`、**`epoll_ctl05`**、
  `epoll_wait01`。
- `epoll_ctl05` 此前在 `test/initramfs/.../all.txt` 里被注释掉；本次启用，
  套件计数 767 → 768。它构造五层链后把最外层加回最内层，走的就是新的
  `reaches` 路径，返回 `ELOOP`，说明 DFS 在真实内核路径上生效。
- 板子上旧内核的"环被接受 + 挂死"已复现（见上）；修复后内核的 2-环对照
  实验因板上被挂死进程拖住、需要一次物理复位后补做。

## 影响面

环引用不是内存安全问题，而是：

- 与 Linux 语义不一致（应在 `EPOLL_CTL_ADD` 时拒绝）；
- 活跃环会让 `epoll_wait` 挂死/空转（上例直接挂死），把使用者的 IO 循环
  卡住。桌面环境里任何"用 epoll 监视另一个 epoll"的库代码一旦写错，
  都会命中。
