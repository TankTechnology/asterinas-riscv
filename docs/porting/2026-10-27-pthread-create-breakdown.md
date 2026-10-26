# pthread_create/join 的三段分解（810 µs → 谁最贵）

日期：2026-10-26
内核：`d170e71a4`（本文件写于未改内核的状态；任务 C 的实验版本另行标注）
探针：`/tmp/vb/pthread_breakdown_probe.sh`（guest 内 gcc 编译，N=200，取中位数）

## 1. 任务 A：三段分解

打点方式：同一用户态循环里，`t1` = `pthread_create` 前，`t2` = 子线程第一条指令（写回共享数组），
`t3` = 父线程 `pthread_create` 返回，`t4` = `pthread_join` 返回；每轮记录，最后取中位数。

### 1.1 Asterinas（PT1）

```
PT default stack=0      d12_med=386656 d23_med=-46292 d34_med=228378 total_med=573490 p10=412358 p90=1186315
PT small   stack=131072 d12_med=363124 d23_med=-44388 d34_med=199373 total_med=634534 p10=317945 p90=1042464
PT large   stack=33554432 d12_med=363710 d23_med=-4503  ...
```

| 段 | 中位 | 占本轮 (573.5 µs) |
|---|---|---|
| **t2−t1：内核建线程 + 首次调度到子线程** | **386.7 µs** | **67.4 %** ← 最大 |
| t3−t2：父线程返回 | −46.3 µs（负：子线程先跑起来了） | — |
| t4−t3：`pthread_join` 回收 | **228.4 µs** | 39.8 % |
| 合计 | 573.5 µs | 100 % |

### 1.2 Linux（PT2，同程序同条件）

```
PT default stack=0      d12_med=15457 d23_med=-2978 d34_med=8559 total_med=21037
PT default2 stack=0     d12_med=11303 d23_med=-3072 d34_med=8803 total_med=17054
PT large   stack=33554432 d12_med=10970 ...
```

| 段 | Asterinas | Linux（default） | 倍率 |
|---|---|---|---|
| t2−t1 建线程+首次调度 | **386.7 µs** | 15.5 µs | **25×** |
| t4−t3 join 回收 | **228.4 µs** | 8.6 µs | **27×** |
| 合计 | 573.5 µs | 21.0 µs | 27× |

（上一轮 SB 测到 810 µs 是均值且含 4 组子测试；本轮取中位数、去掉首轮热身后是 573 µs，两者不矛盾，
报告以本轮的中位数为准。）

### 1.3 用户栈大小不是因素

三档用户栈（8 MiB 默认 / 128 KiB / 32 MiB）下 `d12` 分别是 386.7 / 363.1 / 363.7 µs——**几乎不变**。
⇒ 用户栈的分配/清零不是瓶颈（glibc 用 mmap 惰性映射，本来也不清零）。

## 2. 任务 B：源码嫌疑（只读，已给文件:行号）

| # | 嫌疑 | 位置 | 现状 |
|---|---|---|---|
| 1 | **内核栈每线程 512 KiB，分成 32 段分配再逐页映射** | `ostd/src/task/kernel_stack.rs:38`（`DEFAULT_STACK_SIZE_IN_PAGES = 128`）、`:51`（`STACK_SEGMENT_SIZE_IN_PAGES = 4` → 32 段）、`:92-96`（每段一次 `FrameAllocOptions::alloc_segment_with`）、`:110-114`（`KVirtArea::map_frames` 映射 128 页）、`:127-132`（`flush_tlb` 切换时检查一致性） | **未清零**（`:94` `zeroed(false)`），但 32 次段分配 + 128 页映射是每线程固定成本；Linux 内核栈 16 KiB |
| 2 | 调度入队 | `ostd/src/task/scheduler/mod.rs:509` `run_new_task` → `scheduler_singleton().enqueue(...)` | 走 per-CPU runqueue，代码里未见全局锁；仅凭源码不能定罪 |
| 3 | clone 的页表/COW | `kernel/core/src/process/clone.rs:383-390`：线程 clone 只 `clone_handle()` 复用 vmar，**不复制页表** | 与 512 MB fork 的 COW 路径无关，可排除 |
| 4 | 线程创建时的克隆项 | `clone.rs:392-407`：file table、fs、FPU context、namespace proxy、credentials 各一份 | 需要实测才能判断哪一项贵 |
| 5 | join 的唤醒路径 | `pthread_join` 走 futex 等待；futex 往返实测 19.2 µs vs Linux 5.84 µs（3.3×） | 3.3× 解释不了 join 段的 27×，说明还有别的成本（任务退出、状态回收） |

**归类**：`d12`（67 %）在「建线程 + 首次调度」，`d34`（40 %）在「join/回收」；
两者都比 Linux 慢 25–27×，且都不在用户态（用户栈大小无关）。

## 3. 任务 C：单变量实验（进行中）

不改代码的实验入口：`STACK_SIZE_IN_PAGES` 由编译期环境变量 `OSTD_TASK_STACK_SIZE_IN_PAGES` 决定
（`kernel_stack.rs:24-35`）。用 `OSTD_TASK_STACK_SIZE_IN_PAGES=32`（128 KiB）重编一版，
只改这一个变量，重跑同一探针看 `d12` 是否显著下降。

```
PT3: XORG_CLIENT user=False pass=False command=False done=False
     FAIL (the Xorg flow did not complete)
     panic=0，systemd 早期停住（最后一行 "Failed to open libbpf, cgroup BPF features disabled"）
     → 未进入探针，没有任何 PT3 数据
```

**结果：NA（取不到）**。128 KiB 内核栈版本没能引导完（无 panic，systemd 初始化早期停住），
所以这一轮**不能**用来判断「512 KiB 内核栈 + 32 段分配」是不是 `d12` 的主因。
时间盒到点，默认构建已恢复（`target/osdk/asterinas-osdk-bin.elf` 19:13 重新编译为默认 128 页）。

下一步建议（未做）：先用栈水位统计（在 `kernel_stack.rs` 的 guard page 上记录最低使用地址）
测出真实峰值用量，再决定能降到多少页；直接压到 32 页在本轮不可行。

## 4. 证据

- `evidence/2026-10-27-pthread-create-breakdown/PT1-asterinas-probe-lines.txt`
- `…/PT2-linux-probe-lines.txt`
- PT3（32 页内核栈实验）：**无数据**（引导未完成，见 §3）；控制台 `~/vb-ws/PT3/console.txt`
- 控制台 `~/vb-ws/PT{1,2,3}/console.txt`
- 探针 `/tmp/vb/pthread_breakdown_probe.sh`
