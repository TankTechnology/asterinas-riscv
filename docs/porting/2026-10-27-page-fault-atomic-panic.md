# 修 page-fault panic：`might break atomic mode (preempt_count = 3)`

日期：2026-10-27
内核：修复前 `245594cbc399ce04`，修复后见 §3

## 1. 现象

Firefox 滚动阶段偶发（约每 3 次运行 1 次）panic：

```text
Uncaught panic: This function might break atomic mode
  (preempt_count = 3, is_local_irq_enabled = true)
  at kernel/core/src/vm/vmar/vmar_impls/page_fault.rs:10
```

`page_fault.rs:10` 是 `Vmar::handle_page_fault` 的首行 `self.inner.read()`。

## 2. 任务 A：复现与上下文

### 2.1 先做定向复现（未触发）

`sock_pingpong` 式的「持锁拷贝 + 缺页」不是充分条件。写了
`tools/x86/debian/pf_repro_check.sh`：sender 用**从未触碰**的匿名页做
`sendmsg` 源（缺页恰好发生在 socket 写路径持锁拷贝期间），四种模式：

| 模式 | 负载 | 结果 |
|---|---|---|
| plain | 单线程，1 MiB 页，12,000 次 | 无 panic |
| threads | 4 线程，256 KiB 页，各 12,000 次 | 无 panic |
| press | threads + 768 MiB 常驻（制造堆/帧压力） | 无 panic |
| churn | threads + 1 线程反复 mmap/munmap 64 MiB（拉长 Vmar 写锁持有） | 未跑完即停（见 2.2） |

结论：**原子上下文里的用户缺页本身不会 panic**，还需要 `Vmar` 写锁的
真实竞争。plain/threads/press 都只是让缺页发生在 preempt_count>0 的
拷贝里，但当时没有写者在 Vmar 锁上。

### 2.2 诊断插桩抓到真相

在 `page_fault_handler`（`kernel/core/src/thread/exception.rs`）加一条
限流日志：凡是 `preempt_count != 0` 的用户缺页，打印
`preempt_count / fault addr / 当前 syscall nr / user sp / user pc`
（syscall 状态用新增的 `Thread::try_syscall_state()` 非阻塞读取，
避免在故障路径上再抢锁）。

启动后 1 秒即出现第一条：

```text
WARN: atomic-mode user fault: preempt_count=1, addr=0x5555556fa000,
      syscall=217, user_sp=0x7ffffff1a248, user_pc=0x7ffffe7be623
```

`syscall=217` 是 `getdents64`：ls/find 之类的目录遍历在**持有 spin 锁**
的情况下把目录项拷进用户缓冲，用户页未映射 → 缺页。即：**「原子上下文
里发生用户缺页」是内核常态操作**，并非异常路径。

那么 panic 的条件就清楚了：

1. 系统调用持有若干 spin guard（`preempt_count` 已 >0，现场是 3，
   例如 socket 的 `state` RwLock 覆盖了用户拷贝）；
2. 拷贝触发的用户缺页进入 `Vmar::handle_page_fault`；
3. 该函数第一行 `self.inner.read()` 是 **`RwMutex`（可睡眠锁）**；
4. 此刻另一个线程正在 `mmap`/`munmap`/`mprotect`（持有写锁）→ 读锁要
   排队 → `WaitQueue` 把任务挂起 → `switch_to_task()` → `might_sleep()`
   在 `preempt_count=3` 下 panic，报点就是第 3 步那一行。

这解释了为什么只在 Firefox 下出现：它同时有大量 `mmap/munmap`
（JIT、线程栈、IPC 缓冲）与大量带缓冲区的 syscall，锁竞争窗口才够大。

## 3. 任务 B：修法

**原子模式下绝不阻塞在 MM 锁上**（对应任务书"原子上下文 + 用户地址 →
走 fault-safe 返回"的选择）：

```rust
// kernel/core/src/vm/vmar/vmar_impls/page_fault.rs
let inner = if ostd::task::atomic_mode::preempt_count() == 0 {
    self.inner.read()
} else {
    self.inner.try_read().ok_or_else(|| {
        Error::with_message(Errno::EAGAIN, "the Vmar is locked in atomic mode")
    })?
};
```

- 原子模式（`preempt_count > 0`）且 `Vmar` 锁**未被占用**：照常处理缺页，
  行为与之前完全一致（这是绝大多数情况，诊断日志里 getdents64 的那次就
  属于此类，不需要任何退化）。
- 原子模式且锁**被占用**：返回 `EAGAIN`，上层按 fault-safe 路径让该次用户
  拷贝返回 `EFAULT`，**不再挂起内核**。
- 非原子模式：行为不变。

配套改动：

- `ostd/src/task/atomic_mode.rs`：新增 `pub fn preempt_count() -> u32`。
- `kernel/core/src/thread/mod.rs`：新增 `Thread::try_syscall_state()`。
- `kernel/core/src/thread/exception.rs`：限流（32 条）的原子模式缺页日志
  （`atomic-mode user fault: ...`），保留作为后续排查线索。
- `PageFaultInfo::address` 由 `pub(in vmar)` 放宽为 `pub(crate)`（诊断用）。

为什么不在 socket 路径去掉 spin lock：`UnixStreamSocket::state` 的读锁
覆盖了整个 `try_write`（含用户拷贝），改成可睡眠锁或先取 Arc 再放锁都
是更大的重构；本轮先把"原子模式下永不睡"钉死在 MM 层，socket 侧的结构
问题记入后续。

## 4. 任务 C：回归（内核 `69f3feada57f598a`）

### 4.1 定向缺页负载

`pf_repro_check.sh` 四种模式（plain / threads / press / churn）在修复内核上
全部跑完，panic 0。注意：这四种负载在**修复前**也没有复现 panic，所以它们
只能作为"没有引入退化"的证据，不能证明修复有效（真正复现条件是 Vmar 写锁
恰好被占用的窄窗口）。

### 4.2 Firefox 10 轮滚动

`tools/x86/debian/pf_firefox10_check.sh`（1024×768、openbox、
每轮 10 s Page_Down）：

- 窗口存在（`win=6291457`），10 轮 marker `pff_r1`…`pff_r10` 全部出现；
- **panic = 0**，`DAILY_DONE=ok`；
- 每轮 top1 分别是 read / read / ioctl / poll / syscall_179 / execve×4 /
  madvise —— 第 5 轮之后看起来是 Firefox 的加载/重启循环（每 10 s 40 次
  `execve`），仍然对进程创建与 IPC 路径构成压力，但不是稳定的滚动负载。
- 条件概率参考：修复前约 7 次 Firefox 运行里 panic 2 次；本次 10 轮 0 次。
  由于触发需要 Vmar 写锁竞争，"0 次"是支持性证据而非证明。

### 4.3 矩阵

见 §4.4（7 域）。

### 4.4 7 域矩阵

| 域 | 结果 | 细节 |
|---|---|---|
| DESKTOP | ok | Xorg+DRI3 |
| APP | ok | xterm |
| APT | ok | xclip+xterm+alsa-utils-installed |
| CLIPBOARD | ok | xclip-round-trip |
| AUDIO | ok | aplay-hw:0,0 |
| USB | ok | mounted-/dev/sda-read-ASTERINAS-USB-OK |
| SUSPEND | ok | woke-up-disk-and-io-ok |

额外域 INPUT_METHOD 仍 fail（`engine--no-text`，既有缺口）；
`DAILY_DONE=ok`、panic 0、allocation failed 0。

## 5. 限制与后续

- 原子模式 + Vmar 写锁被占用时，该次用户拷贝返回 `EFAULT`（调用方看到
  一次失败）。窗口很小（mmap/munmap 的临界区），但语义上仍是一次退化。
- 根治方向：让 socket/pipe 等路径在拷贝用户内存时**不持有 spin lock**
  （例如把 `state` 换成可睡眠锁，或先取出 `Arc<Connected>` 再放锁）。
- 诊断日志目前保留；若确认稳定，可降级为 `debug!` 或删除。

## 6. 复现

```sh
# 定向缺页负载 + Firefox 10 轮
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/pf_diag_check.sh make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/ff-lag/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm DAILY_USE_WORKSPACE=$PWD/target/pffix
```
