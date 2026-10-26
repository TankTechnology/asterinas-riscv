# fork/clone 成本：82.6 ms/次是真的，来源是逐页 COW 复制

日期：2026-10-27
内核：修复前 `69f3feada57f598a`，优化后 `16dcb23215158731`

## 0. 结论先行

- **82.6 ms/次是真的**：Firefox 内容进程 fork 时映射了约 150 MB，
  Asterinas 的 fork 按页复制页表，**每页约 2.0 µs**，与映射大小成正比。
- 与子进程生命周期无关：`clone` 的 CPU 计时在父进程返回时结束；微基准里
  子进程 `_exit()` 立即退出，测得 1.7 ms/次（小进程），与 mapping 大小
  无关的固定开销约 1.7–1.8 ms。
- 分解（512 MB、131072 页）：固定 1.84 ms + 源侧 COW 写保护 ≈90.7 ms
  （0.69 µs/页）+ 目的页表复制 ≈180 ms（1.38 µs/页）。
- 本轮优化（省掉每页一次 find + 冗余 jump）：见 §3。

## 1. 任务 A：确认真实性（`tools/x86/debian/fork_bench_check.sh`）

微基准（guest 内 C 程序；`/proc/syscall_time` nr=56 的 cpu 差值 ÷ 次数）：

| 负载 | wall/次 | **clone CPU/次** | clone 计数 |
|---|---|---|---|
| fork ×10,000（子进程立即 `_exit`，父 waitpid） | 3.42 ms | **1.71 ms** | 10,000 |
| thread ×10,000（pthread_create/join） | 1.96 ms | —（glibc 用 clone3=435，非 56） | 0 |

- 小进程 fork 就要 1.71 ms，比 Linux 的几十 µs 高一个数量级；
- 若计时把子进程生命周期算进去，1 万个"即死"子进程不可能每个都花 1.7 ms，
  且 wall（含 fork+waitpid）3.42 ms 说明耗时确实在父进程的 clone 调用里。
- Firefox 的 82.6 ms/次 = 1.7 ms 固定开销 + 约 150 MB×0.55 ms/MB。

## 2. 任务 B：成本分解

新增两个探针（同一基准）：

- `reserve <MB>`：mmap 但不触碰（无 PTE）后 fork → 只测 VMA/固定开销；
- `protect <MB>`：触碰后 `mprotect(PROT_READ)`，即"源侧逐页写保护 + TLB flush"
  的等价成本。

| 负载 | 64 MB | 256 MB | 512 MB |
|---|---|---|---|
| fork（已触碰内存） | 37.5 ms | 143.5 ms | **272.8 ms** |
| reserve（未触碰） | 1.62 ms | — | **1.84 ms** |
| mprotect(PROT_READ) | 12.3 ms | — | **90.7 ms** |

推论：

1. **不是 VMA/深拷贝问题**：reserve 512 MB 与空进程几乎一样（1.8 ms）。
2. **按页线性**：272.8 ms / 131072 页 = **2.08 µs/页**；mprotect 90.7 ms →
   **0.69 µs/页**（源侧 protect + flush）。
3. fork 的**目的页表复制**部分 ≈ 272.8 − 1.84 − 90.7 = **180.3 ms**
   （**1.38 µs/页**）。
4. 固定开销 ~1.8 ms（fd 表、信号、VMA 结构、TLB 全刷等），对小进程是主导。

耗时分解表（512 MB fork, 272.8 ms）：

| 成分 | ms | 占比 | 每页 |
|---|---|---|---|
| 固定开销（无映射页时） | 1.8 | 0.7% | — |
| 源侧 COW 写保护（含 TLB flush） | 90.7 | 33% | 0.69 µs |
| 目的页表复制（map + 游标走查） | 180.3 | 66% | 1.38 µs |

每页 ~2 µs 的绝对值本身就是问题（Linux 同类工作约 50–100 ns/页），说明
**游标 API 的每页开销**（多次 find/query/jump + 条目写入）是根因，而不是
缺 COW（COW 已经有了：只复制 PTE，不复制物理页）。

## 3. 任务 C：优化（单变量）

`cow_copy_pt`（`kernel/core/src/vm/vmar/vmar_impls/fork.rs`）每页做：

1. `src.find_next()` → 2. `src.query()` → 3. `src.protect_next()`（内部**又 find 一次**）
   → 4. `dst.jump(va)` → 5. `dst.map()`。

改动（两处，都是省掉每页的冗余工作）：

- 新增 `CursorMut::protect_cur()`（ostd）：对**当前**条目做 protect 并前进，
  不再重新查找；fork 命中路径改用它，**每页少一次 find**。
- `dst.jump(mapped_va)` 加守卫：`map()` 本身已经前进游标，连续页不再重复
  jump。

改前/改后（同一基准、同一镜像、4 vCPU）：

| 指标 | 改前 `69f3feada` | 改后 `16dcb232` | 变化 |
|---|---|---|---|
| fork 小进程（×10k） | 1.71 ms | **1.52 ms** | **−11.2%** |
| fork 64 MB | 37.5 ms | 30.6 ms | −18.4% |
| fork 256 MB | 143.5 ms | 114.0 ms | −20.5% |
| fork 512 MB | 272.8 ms | **226.7 ms** | **−16.9%** |
| 每页（512 MB） | 2.08 µs | **1.73 µs** | −16.8% |
| reserve 512 MB（固定开销） | 1.84 ms | 1.65 ms | −10.3% |

**没有达到“fork < 1 ms”的目标**：小进程 1.52 ms，512 MB 进程仍有 226 ms。
本轮去掉的是每页一次冗余 find 和一次冗余 jump，占每页成本约 17%；
剩下 1.7 µs/页 的大头在游标/页表条目操作本身（每次操作都要走查层级、
写条目、维护 RCU），以及固定开销 1.5–1.7 ms（fd/信号/cgroup/TLB 全刷）。
要达到 1 ms 级需要按页表页批量复制（一次 512 条 PTE），属于结构性改动，
不在本轮范围。

## 4. 回归

7 域矩阵（内核 `16dcb232`）：见 §4.1。

### 4.1 7 域矩阵

| 域 | 结果 | 细节 |
|---|---|---|
| DESKTOP | ok | Xorg+DRI3 |
| APP | ok | xterm |
| APT | ok | xclip+xterm+alsa-utils-installed |
| CLIPBOARD | ok | xclip-round-trip |
| AUDIO | ok | aplay-hw:0,0 |
| USB | ok | mounted-/dev/sda-read-ASTERINAS-USB-OK |
| SUSPEND | ok | woke-up-disk-and-io-ok |

INPUT_METHOD 仍 fail（既有缺口）；`DAILY_DONE=ok`、panic 0、allocation failed 0。

## 5. 限制与后续

- 真正的量级差距（每页 ~2 µs vs Linux ~0.1 µs）需要**按页表页批量复制**
  （一次生成 512 个 PTE），游标 API 目前只支持单条目操作，这是下一步的
  结构性改动。
- fork 的固定开销 1.7–1.8 ms 也远高于 Linux，值得单独分解（fd 表、信号、
  cgroup、TLB 全刷）。

## 6. 复现

```sh
DAILY_USE_CHECKLIST=$PWD/tools/x86/debian/fork_bench_check.sh make test_x86_daily_use \
  X86_DEBIAN_KERNEL=$PWD/target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_XORG_BASE_ROOT=$PWD/target/ff-lag/daily.ext2 \
  X86_DEBIAN_XORG_ACCEL=kvm DAILY_USE_WORKSPACE=$PWD/target/fk
```
