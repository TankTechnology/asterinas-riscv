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
