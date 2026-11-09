# Performance audit and tick change, 2026-11-09

The desktop "felt slow overall". Rather than A/B whole images, this measures
where the time actually goes, then changes the costs that belong to the OS.

## Method

All numbers come from the booted board with the serial console as the only
control channel:

- `/proc/stat` deltas for user/system/idle ticks, interrupts and context
  switches over a fixed window;
- `/proc/<pid>/stat` deltas for per-process attribution;
- a fixed 5 s window per workload, with the workload in the guest;
- synthetic input through X11 (`XWarpPointer`, XTEST keys, button events) so
  process startup is excluded;
- `x11perf` for X round-trip and pixel throughput;
- `strace -c` at console log level 1, because the `wait4(__WALL)` warning
  used to print a line per wait and dominated the trace;
- a small C probe that splits fork into "child starts", "child runs",
  "child exits", "parent wakes".

## What the measurements say

Idle, 5 s window, Firefox idle on the desktop:

| Metric | Before | After |
| --- | ---: | ---: |
| Timer IRQs | ~4,900/s | ~1,225/s |
| System time | 0.48 s / 5 s | 0.05 s / 5 s |
| Context switches | 276/s | 274/s |

Interactive workloads (5 s windows):

| Workload | CPU | Split |
| --- | ---: | --- |
| Browser scroll (200 wheels) | 4.34 CPU-s (0.87 core) | Firefox 2.6 s, Xorg 0.36 s, kernel ~2.3 s |
| One click (synthetic) | ~14.6 ms | Xorg 4.8, openbox 3.5, fcitx5 2.5, pcmanfm 1.8, lxpanel 1.1 |
| One pointer motion | ~1.6 ms | lxpanel 0.9, Xorg 0.15 |
| One keystroke to an unfocused root | 0.08 ms | Xorg + shell only |

Other baselines:

- X round trip 0.34 ms idle, 0.47 ms under scroll load;
- 500x500 scroll blit 0.46 ms (about 2.2 GB/s, memory bound);
- text 2.2 us/char;
- syscalls: getpid 3.9 us, pipe write+read 11.8 us, futex 5 us;
- fork+exit+wait **8.5 ms** (2.2 ms until the child starts, 14 us running,
  1.7 ms from child exit to the parent's wait returning); fork+exec+wait
  12-14 ms; pipe ping-pong RTT 84 us.

So the kernel and X "unit prices" are sane except for two things: the
periodic tick was paying a fixed ~10% of one core when idle, and process
creation costs several milliseconds. The per-event interactive cost lives in
the desktop stack (Xorg, openbox, fcitx, lxpanel, pcmanfm), not in the kernel.

## Changes

1. `ostd::timer::TIMER_FREQ` 1000 -> 250 Hz. Sub-tick wakeups still use the
   RISC-V one-shot deadline, so timer precision is unchanged; the periodic
   tick only drives preemption and accounting. `CLOCK_*_COARSE` resolution now
   reports one tick (4 ms) instead of a hardcoded 1 ms.
2. `wait4` treats `__WALL` as supported. glibc's `waitpid` and ptrace (strace,
   gdb) pass it on every call; the old warning wrote a line to the serial
   console per wait.
3. The cross-CPU handoff message in `ostd` is now `debug!`. It fires on
   ordinary migrations while IRQs are disabled; a console write there stalls
   the CPU for milliseconds. Boot logs show zero of these now.

## Next costs to attack

- **Process creation.** 2.2 ms from fork to the child running, 1.7 ms for exit
  plus wakeup, and 5-6 ms more for exec. Every shell command and application
  launch pays this. The next step is to split the fork page-table copy from
  the child's first scheduling and to profile the exit/teardown path.
- **Desktop stack per event.** A click wakes fcitx5, openbox, pcmanfm, lxpanel
  and Xorg for ~14 ms of CPU. Configuration work (input-method on demand,
  panel plugins) may pay more than kernel changes here.
- **Firefox software rendering** dominates scrolling; it is app-level work,
  not a kernel regression.

## Follow-up: wakeup preemption and placement (same day)

A finer fork probe split the cost into child-start and parent-wakeup and
showed the real shape of the problem:

| Measurement | Before | After |
| --- | ---: | ---: |
| fork+wait average (unpinned) | 4.5-7.5 ms | 1.0-1.1 ms |
| fork+wait worst case | 257-461 ms | 17-18 ms |
| 4 CPU-bound processes | sometimes 3 of 4 harts | all four harts |

Two scheduler defects caused this:

1. **No wakeup preemption.** `ClassScheduler::enqueue` only requested a
   preemption IPI when the new task's policy class outranked the current one.
   A fair task woken on a CPU running another fair task therefore waited for
   the running task to exhaust its whole time slice (the fair class only
   re-evaluates on the tick), which measured as hundreds of milliseconds for
   `fork`+`wait`. `enqueue_entity_at` now reports whether a queued fair entity
   has a smaller vruntime than the current fair task, and `enqueue` requests a
   preemption IPI in that case, matching Linux's wakeup preemption rule.
2. **Placement ignored the running task.** The spawn path compared only
   `queue_len`, which is zero both for an idle CPU and for a CPU running one
   task; new tasks stacked on busy harts and left others idle. It now compares
   `runnable_load()` (queued plus running).

The changes did not regress the other numbers: idle stays at ~1,100
interrupts/s and ~130 context switches/s, the X round trip is 0.34 ms, and a
200-click burst still costs ~68 context switches per click (unchanged).

## Follow-up: where a page fault spends its 30 microseconds

The next bottleneck is the minor page fault: a first touch costs 29-31 us
(best of several rounds; plain wall-clock averages are dominated by desktop
background load), while a userspace 4K memset on populated pages is 0.44 us
and memcpy is 0.76 us. Reading data is not slow; this is kernel work.

The RISC-V time CSR is the 1 MHz timebase, so one tick is one microsecond.
Temporary phase counters around the fault path (removed after measuring)
reported, for a single anonymous fault:

| Phase | Cost |
| --- | ---: |
| exception entry + return to user | ~10 us |
| VMAR mapping lookup | 1 us |
| cursor creation + page-table query | 3 us |
| prepare (frame allocation + metadata + clear) | 12-13 us |
| page-table insert + RSS accounting | 1-2 us |
| total | ~29-31 us |

File-backed read faults are amortized differently: `handle_page_faults_around`
maps up to 16 pages per fault, which is why touching 280 mapped pages of
`/bin/bash` costs only 2.8 us/page even though each fault itself costs ~43 us.

Two hypotheses were checked and rejected:

- **Slow glibc memset.** A per-page memset looked 40x slower than memcpy in
  one binary, but that binary's initial fill had been eliminated as a dead
  store, so the timed loop was faulting every page. With a read-back, memset
  is 0.44 us/4K (9 GB/s) and the "slow memset" disappears.
- **Slow compiler-builtin memset in the frame clearer.** Replacing
  `write_bytes` with volatile 64-bit stores did not reduce the prepare phase
  (12 us vs 13 us), so clearing is not where the time goes; the change was
  reverted.

So ~12 us of the fault is frame allocation and metadata, not zeroing. The
per-CPU frame cache holds only 12 single frames, and every allocation also
runs the pool balancer; a 512-page burst misses the cache almost every time.

### Next steps for the fault path

1. Split `prepare` into pool allocation, metadata initialization and clearing
   with the same timer technique; then optimise the dominant part. Candidate
   fixes: enlarge the per-CPU single-frame cache, batch pool refills, and skip
   `balancing::balance` on the single-frame fast path.
2. Batch anonymous faults the way file faults are batched (map a run of
   pages per fault) behind a sequential-touch heuristic, so the ~10 us trap
   and ~4 us cursor/map overhead are amortised.
3. Measure the exception entry/exit cost separately; it is ~10 us, versus
   ~4 us for an `ecall` syscall, and worth understanding before optimising.
4. The desktop stack remains the other half: a click still wakes five
   processes for ~14-20 ms of CPU, which is userspace architecture work.
