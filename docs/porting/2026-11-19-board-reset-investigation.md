# Megrez board resets — what the 2026-11-19 board session established

Status: **root cause found and fixed (2026-11-20 02:00).** This note now
reads as a case file: what was seen, which hypotheses measurements killed,
what the cause turned out to be, what the fix is, and how to tell whether it
regressed. Every observation names the image and how the boot was
configured, because several earlier conclusions in this investigation were
artifacts of the harness rather than the board.

## 0. Bottom line

**Symptom.** With the default configuration the Megrez board's boot stopped
2-11 s in: one hart (always the BSP) stopped taking interrupts, the other
harts kept running, and the board reset ~31 s later when its hardware
watchdog expired.

**Cause.** Not the firmware, not the reset controller, not a deadlock on a
lock, not the TLB-flush traffic itself. The BSP was alive and spinning at
full speed inside the external-interrupt handler: the USB xHCI host
registered its interrupt line **unmasked** while its upper half only woke a
task thread, and an xHCI line stays asserted until that thread consumes the
event ring. Completing the interrupt let the controller re-raise it
immediately -- about **633,000 interrupts a second** -- so the one thread
that could clear it never ran. That is a livelock, not a hang.

**Fix** (`kernel/comps/usb/src/arch/riscv/mod.rs`): map the line masked,
mask on every delivery, and re-arm only after `poll_report()` has drained
the ring (the point at which `USBSTS.EINT`/`IMAN.IP` are cleared and `ERDP`
advanced). The same pattern the MMC and GMAC drivers already use.

**Result.** Default configuration (remote TLB flushes on, no diagnostic
knobs), three consecutive rounds: `DESKTOP_BOOT_READY` at 15.100 s /
15.184 s / 15.072 s, `storms=0`, no storm line, both USB hosts serving
a device, no reset in the 60 s window (the two rounds that were watched
longer lived 65.6 s and 86.9 s).

**Regression line** -- one grep, run after any change that touches the IRQ
or USB paths:

```
storms=0  and no "ASTERINAS_IRQ_STORM"  and both "USB host loop started"  and DESKTOP_BOOT_READY
```

## 1. Observations

### 1.0 The BSP's clock stopped while other harts kept running

Wall-clock arrival times on the serial bridge, last boot of the b106 round:

```
last line carrying a kernel timestamp  23:37:08.021   ([4.412], also the first stall)
second stall report                    23:37:09.022
pll config ok (reset)                  23:37:09.122
```

The two stall reports are exactly 1.000 s apart on the wire — each waited its
full second — yet the kernel timestamp never moved past 4.412. `Jiffies`
advances only on the BSP (`ostd/src/timer/mod.rs`), so the BSP's clock stopped
at 4.412 s while another hart executed for a further second and printed.

This is the same event as "the target hart's interrupts went silent", seen from
the kernel's own time source rather than from any new instrumentation.

Timing of the reset, for the record: **0.100 s after the second stall report**,
and 1.101 s after the last kernel line. It is neither the 31.2 s watchdog
measured in §1.1 nor 10.7 s.

> **Correction (2026-11-20, §1.6): both numbers are retracted.** They came
> from stamping a line with the first time the log file reached that line's
> *starting* offset, which attributes the silence that follows a line to
> that line. Judged by when each line is *completely written*, the
> spontaneous reset comes about 31 s after the last kernel output -- it is
> the hardware watchdog after all. See §1.6.

**Arrival times are not production times.** Two independent recorders (a
100 ms file-offset poller and a line-accurate reader) both show the last
heartbeat's arrival compressed by ~0.5 s against its own `uptime_ms`
(`kicks=2` -> `kicks=3`: `uptime_ms` advances 992 ms while the arrival gap is
0.512 s). The serial bridge backs up under the dense output just before death,
so a gap measured on arrival is an *underestimate* by up to ~0.5 s. The reset
gap above is therefore arrival-caliber: "about 1 s", not milliseconds.

Rules that follow, and that hold for every later section:

- **Death time comes from the kernel's own clock** -- the last heartbeat's
  `uptime_ms` or the last timestamped log line -- never from arrival.
- An **arrival-caliber** interval says "±0.5 s" out loud. It is fine for the
  order-of-magnitude split (~1 s vs 31 s) and not fine for anything finer.
- The **TSC fields inside one report** (`waited_us`, `*_age_us`) are one
  self-consistent clock read at one instant and are unaffected.


### 1.1 The hardware watchdog times out after ~31.2 s, not 10.7 s

`asterinas.watchdog_stop_kick_after=20` stops the heartbeat's kick at a known
uptime (image b88). The board reset **31.23 s** later (±1 s, one sample),
measured by arrival time on the serial bridge.

Consequence: a reset that follows a stall report by ~0.1 s cannot be this
watchdog.

### 1.2 The SoC reset-status registers are not informative on this path

Immediately after two spontaneous resets (image b104, default configuration):

```
SYS_SWRST_VALUE=0  CLR_RST_STATUS=0  DIE_STATUS=0x3f  CLR_BOOT_INFO=0
SYS_SWRST_VALUE=0  CLR_RST_STATUS=0  DIE_STATUS=0x3f  CLR_BOOT_INFO=0
```

Both reads identical; every meaningful bit clear. Either the block does not
record this reset class, or the U-Boot path clears it. The registers cannot be
used to classify reset sources until that is understood.

### 1.3 A firmware send call is cheap; concurrent senders are not fatal

- `ASTERINAS_SBI_PROBE timebase_read_us=0 version_us=0 ipi_us=7`: the timebase
  is native, a no-side-effect firmware call is under 1 µs, three unicast sends
  take 7 µs together.
- In flight: `sbi_avg_us=2`, `sbi_max_us=9` over minutes.
- Three sender threads at the paced rate ran **5 minutes, 9,618,783 sends, no
  reset** (image b76, `tlb_no_remote_flush=1`, desktop up first).

The earlier "three concurrent senders reset the board in 35 s" result was an
artifact: the stress worker paced itself with a busy spin, and three spinning
kernel threads starve the BSP's tick (`Jiffies` advances only on the BSP in the
timer interrupt). The dry run (no interrupt sent) reset the same way. With a
yielding pace loop, both the dry run and the real run survive.

### 1.4 What the failing hart is doing at a stall (b104, default config)

Stall report fields at the moment two other harts have waited 1 s:

```
target_irqoff_us=none            (not inside a tracked IRQ-off window)
target_traps_since_wait=1..2     (timer-class traps still arrive)
target_ipi_traps_since_wait=0    (no software-interrupt trap at all)
target_activity=idle             (not in the console path)
halt=<hundreds per second>       (the CPU keeps entering the idle loop)
```

`halt` counts entries to `halt_cpu()`, not WFI executions: it proves the target
is executing code, nothing more.

Earlier boots showed a different shape: `target_activity=print_write` with a
1-second age, i.e. inside a console write. Both shapes exist.

### 1.4b A stalled target is inside one console record (b109)

One default-config round on the b109 image (warnings and breadcrumbs as in
§4), death at 4.468 s, produced a single stall report:

```
ASTERINAS_TIMER_MODE mode=sbi

waiter_cpu=1 target_cpu=0 waited_us=1000000
target_activity=print_write  target_activity_age_us=999531
target_trap_age_us=993187    target_traps_since_wait=2  target_ipi_traps_since_wait=0
target_halts_since_wait=0    target_irqoff_us=none
all_activity=0:print_write:999549us 1:idle 2:idle 3:idle
```

The target spent the whole second inside the console write path and never
entered its idle loop. `mode=sbi` says this board has no SSTC, so the timer is
armed with an SBI call 250 times per second per CPU as well.

`target_irqoff_us=none` next to `print_write` is an inconsistency in the
tracker (the print path does hold the device lock with local IRQs disabled),
so `none` must not be read as "interrupts were enabled".

Mechanism that fits the one-second duration: the UART driver's
`TX_POLL_BUDGET` is spent **per `write_str` fragment** — `try_send_diagnostic`
calls `try_send_with_budget(buf, TX_POLL_BUDGET)` and that function restarts
the budget every call. One log record is formatted in many fragments, so a
congested transmitter can cost tens of milliseconds per fragment; the measured
single-fragment maximum is 14.5 ms, and a record with tens of fragments reaches
about a second while holding the console lock.

### 1.4c Tick-rate A/B: halving the firmware timer traffic multiplies survival (b113)

Same image, same configuration, one round each, the only difference being
`asterinas.timer_hz`. Death time is the last heartbeat's `uptime_ms` on the
kernel's own clock; the reset gap is measured to the `pll config ok` line with
a line-accurate recorder.

| | 250 Hz | 25 Hz |
|---|---|---|
| last heartbeat `uptime_ms` | 2,268 ms | 10,840 ms |
| last logger timestamp | 2.512 s | 10.320 s |
| ticks (timestamp x rate) | ~628 | ~258 |
| target's last marker | `bh_after_dp` | `idle`, 1 timer trap in the window |
| reset gap (last kernel line -> `pll config ok`) | 1.101 s | 2.003 s |

Ten times fewer timer interrupts gave 4.8 times longer survival while the
tick count fell only 2.4 times: neither a constant wall time nor a constant
tick budget.

Four rounds in all (A1 250 Hz, B1/B2 25 Hz, C1 250 Hz) sharpen this into a
warning rather than a result. Tick counts at death: **567, 813** at 250 Hz and
**271, 121** at 25 Hz; the 250 Hz rounds lasted 2.76 s on average, the 25 Hz
rounds 7.84 s. A pure per-tick risk would predict ~10x longer life from 10x
fewer ticks; the measured factor is **~2.8x**, and within one frequency the
spread is already **1.8-2.2x**. So the dose model is **weakly supported at
best; the samples are too few and the variance too large** to rule anything
else in or out. Do not build on it.

Measurement note: the 31 s of silence after `pll config ok` is DDR
initialisation in the boot ROM, not reset latency. A polling reader that
stamps whole chunks can attribute that silence to the reset line; the anchor
must be the line's own arrival, and the death time should come from the
heartbeat rather than the last logger line.

### 1.4d Nested interrupt storms are not the mechanism (b115)

Default configuration, death at kernel time 4.464 s, two independent senders
stalled on CPU 0. The stall report now carries `target_bh_depth=`:

```
target_activity=bh_after_dp  target_activity_age_us=1001620
target_trap_age_us=1001620   target_sepc=0xffffffff806a594e
target_traps_since_wait=0    target_bh_depth=1
```

Depth **1** -- the target was inside one level-1 bottom half, not tens or
hundreds. Nested interrupts re-entering the bottom-half dispatcher are
therefore not the mechanism.

Two things this round makes explicit about how to read a stall report:

1. **The last marker is an upper bound on "still alive", not the instruction
   where the hart died.** Here the marker and the last trap share the same
   microsecond, so the hart took that trap and then produced nothing more --
   but it may have continued for some time through code that has no marker.
   "It died at `enable_local()`" is not supported by this data and is
   withdrawn.
2. **"The other harts look fresh" does not rule out a held lock.** A live CPU
   can hold a lock for arbitrarily long without touching any marker. That
   inference is withdrawn too.

Because the ages are read at report time, `target_activity_age_us` and
`waited_us` are comparable: the target's last marker was written **1.6 ms
before the wait began**, not a second before it. The stall report is emitted
one full second after the death -- it is a discovery time, never a death
time.

### 1.4e The first round with the lock instrument: not a lock, and a decay (b116)

Default configuration, death at kernel time 6.040 s (last heartbeat
`uptime_ms=6020`, part of a run that is otherwise 0/3 on the acceptance
criterion). Both independent senders report:

```
target_activity=bh_after_dp  target_bh_depth=1
target_lock=none  target_lock_addr=none
target_traps_since_wait=2    target_ipi_traps_since_wait=0
target_activity_age_us=871868 / 936154
```

This was pre-registered (§3.1) as the negative branch, so **waiting for a
spin lock is excluded for this failure** -- the hart vanished without ever
being recorded in a lock's slow path.

The shape of the disappearance changed, though. Here the target's last
marker is **inside** the wait window: 128 ms (first report) and 64 ms (second)
after the wait began, whereas b115 had it 1.6 ms *before*. The two traps the
counter attributes to the whole second were both taken in that first ~128 ms;
the remaining ~872 ms are silent. At the nominal 250 Hz, ~32 timer traps were
due in those 128 ms. So the loss is **a decay followed by silence**, not a
step: the receive path was already succeeding only 2 times in 32 before it
stopped entirely.

That distinction matters for the remaining candidates: a hart that enters
machine mode and never returns predicts a step (it either takes the
interrupt or it does not); a delivery/coalescing pathology predicts a falling
success rate first. The next instrument is built for exactly this split -- a
read-only view of the target's `MSIP` and `mtimecmp` from the CLINT window
(`ASTERINAS_TIMER_MMIO`, `target_clint=msip=… cmp_lag_ms=…`), which another
hart can read whether or not the target is executing.

### 1.5 The console is heavy, but heavy printing is not necessary for a stall

- Single DW-UART write: `uw_max=14.5 ms`, ending in a swallowed timeout;
  `uw_sum` up to 658,000 µs in one second (two thirds of that second spent
  inside console writes with local IRQs disabled).
- Firmware console (`early_println`, one SBI call per byte): a single write of
  a 496-byte heartbeat measured 53.5 ms.
- However, stalls also happen in seconds whose `uw_sum` is below the median,
  and in boots whose longest write was 4.7 ms.

### 1.6 The reset timing was measured wrong; there are two classes (2026-11-20)

Every "reset 0.1-1.1 s after the stall" number in this note was produced by
stamping a line with the first moment the log file reached the line's
*starting* offset. A silence after a line then lands on that line's stamp,
and the whole gap is attributed to the line before it. Stamping a line by
when it is *completely written* (`size >= offset + len`) removes the error.

Measured that way, over the last six resets, the gap between the last
serial output and the boot ROM's `pll config ok` falls into two disjoint
classes:

| `pll` arrival | silence before it | last heartbeat | what it is |
|---|---|---|---|
| 00:56:55 | 30.8 s | 00:56:22 | **hardware watchdog** |
| 01:02:34 | 32.0 s | (none: panicked at 0.4 s, before arming) | watchdog after a pre-arm panic |
| 01:07:17 | 3.0 s | 01:07:13 | **`eic770x_cold_reset`** after a post-arm panic |
| 01:10:33 | 3.0 s | 01:10:29 | cold reset after SysRq |
| 01:13:27 | 2.9 s | 01:13:23 | cold reset after SysRq |
| 01:16:55 | 30.8 s | 01:16:22 | **hardware watchdog** |

The ~3 s class is the vendor reset path: `eic770x_cold_reset()` sends
`CMD_RESTART` to the carrier-board MCU, calls `sbi_timer_mdelay(3000)`, and
writes `SYS_RESET`. The ~31 s class is the DesignWare watchdog calibrated at
31.23 s (§1.1), firing because the stopped BSP stopped kicking it.

So there is **no mysterious reset agent and no 0.1 s reset**: a spontaneous
death is followed by the board's own watchdog about 31 s later, and a 3 s
gap means our own panic/SysRq path ran. This also retires the earlier
exclusion "the reset is not the watchdog": it is, for every spontaneous
case. The watchdog is the only thing that brings a stalled boot back.

### 1.6b The kernel's own clock runs 4.5% slow, so state which clock a number is on

Measured on 2026-11-20 over six hours of one boot: kernel `uptime_ms` advanced
2,007.0 s while the serial arrival stamps advanced 2,096.6 s, a ratio of
0.9573 -- and over 22,119 consecutive heartbeats the `uptime_ms` delta was
1004 ms at the median and mean, with fewer than 0.1% below 1000 ms. Ticks are
therefore not being lost; the kernel's *second* is longer than the wall
second. Both `Jiffies` and the TSC-based `*_age_us` fields scale by the same
device-tree `timebase-frequency`, so a wrong declared frequency shifts both
together -- which is what was observed -- while a tick-accounting loss would
shift only `Jiffies`. The declared 1 MHz is therefore about 4.5% high
(~0.955 MHz real); confirming it needs a read-only `rdtime`-versus-wall
measurement, which was not taken.

Consequences for reading this note: `uptime_ms` and `*_age_us` are *kernel*
time (multiply by ~1.045 for wall time); the 31.23 s watchdog calibration is
wall time (about 29.8 s of kernel time). The `Jiffies`/deadline catch-up
mismatch (`ELAPSED += 1` per interrupt while `advance_periodic_deadline`
skips k periods) is a latent defect worth at most 0.1% tonight and was left
alone deliberately.

The kernel-side clocks (`uptime_ms`, `*_age_us`, `waited_us`) are unaffected:
they are single TSC reads taken inside one report, not serial arrivals.

### 1.7 The "silent hart" was a PLIC interrupt storm (b122)

Three stall reports in one boot, all naming CPU 0 as the target:

```
target_activity=plic_claim   target_activity_age_us=0 / 2 / 3
target_last_ext_irq=5        target_irq_kind=external
target_ext_irqs = 675,823 -> 676,266 -> 702,206
target_traps_since_wait=0    target_halts_since_wait=0
```

The ages are the decisive part: the marker was written **microseconds**
before each read, so the hart was not stopped -- it was executing at full
speed. Between the second and third report (about 41 ms apart by
`target_trap_age_us`) the external-interrupt counter rose by 25,940, i.e.
about **633,000 interrupts per second on one PLIC source, number 5**.

Every earlier observation follows from this without a "dead hart":

| Observation | Explanation |
|---|---|
| `target_traps_since_wait=0` | the hart is inside one trap handler the whole time |
| `target_halts_since_wait=0` | it never returns to the idle loop |
| `target_settimer_age_us` ~1 s | it never returns to the timer code |
| the marker stays where the previous round put it | without the `plic_claim` marker, the last marker before the storm was the last thing it did |

So `handle_irq(SupervisorExternal)`'s unbounded `while claim` loop is the
mechanism. A level-triggered source whose handler cannot quiesce it keeps
the line asserted, and the hart spins there with local IRQs off: no timer,
no IPI, no progress, and from the outside indistinguishable from a hart
that stopped.

The loop is now bounded (`EXTERNAL_IRQ_BUDGET = 4096` per trap). On
exceeding it the line is completed, then its source alone is masked at the
PLIC, one line is printed (`ASTERINAS_IRQ_STORM line=<n> served=<n>
action=masked`), and the handler returns. The mask is permanent by design:
a source that cannot be quiesced must not be invited back. The heartbeat
carries `storms=` so a later round sees it even if the line is lost.

Identifying source 5 is the next step; the UART's receive path is the
current suspect because it defers work to a taskless that cannot run while
the hart is inside that loop.

### 1.8 The storm is the USB xHCI driver, and the bound rescues the boot

The naming instrumentation (`ASTERINAS_IRQ_ALLOC`/`ASTERINAS_IRQ_MAP`) gave
the whole software-to-hardware IRQ table in one boot:

```
num=0 ipi.rs:40      num=2 dw_apb.rs:571  plic=100 (UART)   num=4 mod.rs:466 plic=85 (USB host 0)
num=1 mod.rs:105     num=3 device.rs:97   plic=61  (GMAC)   num=5 mod.rs:466 plic=86 (USB host 1)
```

The first storm of that boot was **line 4 -- USB xHCI host 0**; the round
before it had named line 5 -- USB host 1. Both USB hosts storm, whichever is
reached first.

The cause is in this kernel, not the firmware. `kernel/comps/usb/src/arch/riscv/mod.rs`
registered the xHCI interrupt with the *unmasked* mapping and an upper half
that only wakes the task thread:

```rust
irq_line.on_active(move |_| { waker.wake_up(); });
let _irq_line = irq_chip.map_fdt_pin_to(interrupt_source, irq_line);
```

The xHCI line is level-triggered and is only quiesced when the event ring is
consumed -- in the task thread. With the source left unmasked, completing
the interrupt in the handler lets the controller re-raise it at once, the
hart spins in the claim loop (633,000 IRQ/s), and the one thread that could
clear `USBSTS.EINT`/`IMAN.IP` can never run. The MMC and GMAC drivers use
the masked mapping plus `rearm()`, which is why they never showed this.

With the claim loop bounded (§1.7), the same boot reached the desktop under
the **default configuration** -- remote TLB flushes on, no other knob --
`DESKTOP_BOOT_READY` at 15.052 s, and the round passed its 60 s survival
check. `storms=2` in the heartbeat: two sources were masked (both USB
hosts), and the second storm line was lost in the systemd output while the
counter still recorded it.

The real fix, in the USB driver, is the MMC pattern: map masked, mask on
every delivery, and `rearm()` only after `poll_report()` has drained the
ring (at which point `handle_event()` has cleared `USBSTS.EINT`, cleared
`IMAN.IP`, and advanced `ERDP` with `EHB`). Masking a source permanently is
a survival measure with a cost -- that USB host is then unusable -- so the
bound stays as the backstop while the driver stops needing it.



## 2. Exclusions
| Hypothesis | Killed by |
|---|---|
| Reset is the 31.2 s watchdog | ~~Measured timeout 31.2 s; last kick 1–2 s before the stall~~ **Withdrawn (§1.6): the spontaneous reset *is* the watchdog. It fires ~31 s after the last kick; the earlier "0.1 s" gap was a measurement artifact.** |
| Concurrent IPI senders are fatal | Yielding pace: 5 min, 9.6 M sends, survives |
| "Hart 0 is stuck in a 1 s IRQ-off window" | b104: `target_irqoff_us=none`; the earlier 1 s reading came from a forgotten IRQ-off guard in the idle path |
| "Continuous printing over 1 s" | Stall seconds have `uw_sum` at or below the median |
| The reset-status register identifies the source | All-zero on two spontaneous resets |
| The target is spinning for a lock | b116 `target_lock=none`; with the `RwLock` slow path added, `rw_spin` never fired either -- so **no lock of either kind**, not just no `SpinLock` |
| Nested interrupt storms | b115, `target_bh_depth=1` from two independent senders |
| Concurrent entry into machine mode | `sbi_ipi_serialize=1` (firmware send calls serialized): the stall began at the same time as the baseline (~3.44 s vs ~3.46 s, both derived from `waited_us`; 1.6 ms apart) |
| The stall path's forced IPI resend causes the reset | `ipi_no_resend=1`: death and reset both unchanged |
| WFI with `SIE=0` sleeps forever | `wfi_enable_first=1` did not remove the stall; it moved the death point from `idle` to `irq_enter` and the run died earlier (2.35 s; n=1) |
| The UART receive path | `ASTERINAS_UART_RX ... irq=2`, while the storm was on line 5 |
| An unmapped PLIC source storming | `claim_interrupt` returns `None` for sources without an `IrqLine`; an unhandled source is claimed once and then stays quiet |
| "The hart is dead" | **the decisive correction**: `plic_claim` age of 0-3 us in three samples -- the hart was executing at full speed (§1.7) |

## 2.5 What the firmware itself can do to a hart (OpenSBI reading)

Read against the local OpenSBI tree (`/home/arch-anjie/Program/renode-dl/opensbi-src`,
v1.9; the board reports v1.5, so these are upstream semantics, not proof of the
board's exact binary).

The board reports `MIDELEG = 0x2666`: SSIP/STIP/SEIP are delegated, **MSIP is
not**, so every inter-processor interrupt is taken in machine mode. Machine
mode clears `mstatus.MIE` while it runs, so a hart inside a machine-mode call
takes no machine-mode interrupt — including its own IPI and its own timer.

Four places in that source can keep a hart in machine mode long enough to
explain a one-second, all-interrupt silence:

1. **Console putc spins forever on the UART transmitter.**
   `lib/utils/serial/uart8250.c`: `uart8250_device_putc` is
   `while ((LSR & THRE) == 0);` with no bound. The legacy `CONSOLE_PUTCHAR`
   call (`sbi_ecall_legacy.c`, `sbi_putc` → `nputs` → `console_putc`) reaches
   it **one byte at a time**, and our `early_println` uses exactly that call.
   Measured cost of one heartbeat line on this board: 53.5 ms. If THRE stops
   arriving, the hart is parked in machine mode with interrupts masked.
2. **One missed IPI becomes permanent.** `lib/sbi/sbi_ipi.c`: the sender sets
   a bit in the target's `ipi_type` and writes the MSIP **only when that bit
   was clear**. The bit is cleared only by the machine-mode handler. A hart
   that is not taking machine-mode interrupts leaves the bit set, so every
   later send — including the stall path's forced resend — is folded into an
   interrupt that will never arrive.
3. **Remote fence/TLB sync spins in machine mode.**
   `lib/sbi/sbi_tlb.c`: `tlb_sync` loops `while (atomic_read(tlb_sync) > 0)`
   with no timeout, and the FIFO-full path retries with a comment that the
   two harts "may busy loop leading to a deadlock". Our kernel reaches this
   only through `flush_icache` (syscall 259 and membarrier on the current
   tree), not on the boot path.
4. **An unexpected machine-mode trap parks the hart.**
   `lib/sbi/sbi_trap.c`: `sbi_trap_error` prints through the same console and
   then calls `sbi_hart_hang()`. If the console is the thing that is stuck,
   the hang message is invisible and the hart never returns.

What none of these explain is the reset itself: a hart parked in machine mode
does not reset the SoC, and the board's own watchdog timeout was measured at
31.2 s. The 0.1 s-after-stall reset remains an independent unknown.

A cheap discriminating experiment follows from (1): make the kernel stop
printing through the firmware console once its own UART driver is up, then
see whether stalls disappear. That removes the only route in which our code
enters machine mode thousands of times a second.

## 3. Open question, narrow

> **Answered (§1.7/§1.8):** the target was not stuck in a delivery
> question at all -- it was inside one external-interrupt handler, servicing
> a storming USB xHCI source. The alternatives below were the right
> candidates for the evidence available at the time and are kept as the
> record of how the question narrowed.

At a stall the target hart is executing, is not in a tracked IRQ-off window,
still takes 1–2 timer traps in the second, and takes **zero**
software-interrupt traps. The question is therefore one of:

1. `sip.SSIP` is never set — the firmware's send call returned success without
   writing the MSIP, e.g. because the target's `ipi_type` bit was already set
   (OpenSBI writes MSIP only when the bit was clear, and a forced resend does
   not change that);
2. `sip.SSIP` is set but the core does not take the interrupt;
3. `sie.SSIE` was cleared by mistake.

First reading from that snapshot (b106, default configuration, death at 4.4 s;
both stall reports in that boot have `target_activity=idle`):

```
target_activity=idle  target_irqoff_us=none
target_irq_state=sip=0x0 sie=0x222 on=1 scause=0x8000000000000001 age_us=3265764
target_trap_age_us=1001005  target_traps_since_wait=0
target_ipi_traps_since_wait=0
```

The timer runs at 250 Hz (`ostd/src/timer/mod.rs:25`), so a CPU in this state
should take about 250 timer traps in the one-second window with `0` reported:
a live CPU takes about 1 %. The target's last trap was itself a software
interrupt (`scause` interrupt bit + 1), taken right at the start of the window.

So the observation is not "the target does not receive this IPI". It is:
**after taking that one interrupt, the target took no trap of any kind for a
full second — the timer included.** The IPI is only the first visible symptom.

The `sip=0` snapshot has `age_us≈3.27 s` and describes a different moment (a
CPU entering idle with a pending call 3.27 s earlier). It shows the state
exists; it does not describe the fatal window, and it is not evidence for any
one of the causes below.

Only three states produce complete silence while the tracker shows no IRQ-off
window:

- the CPU stopped executing — partly contradicted by the halt counter moving;
- it executes with `sstatus.SIE=0` inside a critical section whose entry the
  tracker missed (worth auditing on its own);
- it entered machine mode and did not return — an SBI call stuck in firmware.
  Machine mode masks supervisor interrupts, and `Jiffies` advances only on the
  BSP, which would also explain a clock that stops. `inter_processor_call`
  holds local interrupts disabled across its send call (`ostd/src/smp.rs`).
  A 45 ms IRQ-off window was logged with `single_instr.rs:150` as its site,
  but that site is a per-CPU variable load's `disable_local()`
  (`ostd/src/cpu/local/single_instr.rs`), not an `ecall`; it does not support
  this branch and is recorded only as an unexplained window.

Two firmware facts sharpen the "machine mode" branch. The board reports
`MIDELEG = 0x2666`, which delegates SSIP/STIP/SEIP but **not MSIP**, so every
IPI is taken in machine mode. OpenSBI sets a per-hart `ipi_type` bit and writes
the MSIP only when that bit was previously clear; the bit is cleared in the
machine-mode trap handler. A hart stuck inside a machine-mode call therefore
cannot clear it, and every later send — including the stall path's forced
resend — is silently folded into an interrupt that will never arrive. Our
kernel also calls SBI remote `fence.i` from `flush_icache`
(`ostd/src/arch/riscv/mod.rs`), and OpenSBI's TLB sync is an unbounded spin, so
an ecall that does not return is a concrete candidate rather than an abstract
one.

The next instrumentation should therefore be stage breadcrumbs, not another
interrupt-state sample: record a per-CPU stage id at each step of the
inter-processor call (entered, lock taken, callback queued, callback run,
before ecall, after ecall, exited) and print the target's stage in the stall
report. That names the step the target stopped at; a `sip` sample can only say
whether the interrupt arrived.

Answerable by one snapshot: in the target's idle path, when its own pending-IPI
flag is set, store `sip`/`sie`/`sstatus`/last `scause` per CPU; print the
target's copy in the stall report.

Two notes on reading it. The snapshot is taken **with local IRQs enabled**,
just before the idle path disables them, so a snapshot showing `sip.SSIP=1`
means the interrupt is pending and enabled while the core does not take it —
a stronger statement than anything taken from an IRQ-off context. And
`target_irq_state=none` is a result, not missing data: it means the target
never entered its idle path while this CPU's call was pending, so it was not
idle at all. A static review found `sie.SSIE` set in
`ostd/src/arch/riscv/irq/ipi.rs:42,54` and never cleared, and `sip.SSIP`
cleared at trap entry in `ostd/src/arch/riscv/trap/mod.rs:130`.

### 3.1 The remaining general blind spot: lock spins

Every marker above names a *code region*. A hart spinning for a lock sits in
a critical section that no marker describes: with local IRQs off (in a trap
handler, or under a `LocalIrqDisabled` guard) it takes no traps, and it is
not in any of the regions the markers cover -- exactly the picture of a hart
taken away by the firmware. One instrument separates those two cases for all
spin locks at once, without enumerating them:

- `SpinLock::lock`'s slow path records `ACT_LOCK_SPIN` plus the caller's
  `&'static Location` (`#[track_caller]`) and the lock's address, and clears
  both the moment the lock is acquired. The fast path is untouched.
- The stall report prints `target_lock=<file:line>|none` and
  `target_lock_addr=0x...|none`.

Reading it is binary: `target_lock` set with a ~1 s age means the hart is
**waiting for that lock right now**, and the site names the line; the address
tells two CPUs on the same instance (a mutual wait) from two CPUs on
different ones. `target_lock=none` with the activity still at `bh_after_dp`
means the hart vanished without ever waiting for a lock, which leaves the
firmware (machine mode) as the candidate.

**Pre-registered prediction, written before the first `lock_spin` round runs
(2026-11-20 00:40):** if a stall shows `target_lock=none` while the target's
activity is still `bh_after_dp`, the spin-lock hypothesis for this failure is
**excluded on the spot**, and the next round moves to machine-mode
preemption or an S-mode tick source instead of returning to locks. If
`target_lock` is set, the same report must also include the other CPUs'
`act=` and their own `target_lock`/`target_lock_addr` readings, because a
second CPU spinning on the same address is a mutual wait and one spinning on
a different address is not.

Known side effect: the console device table is itself a `SpinLock`
(`kernel/core/comps/console/src/lib.rs`), so a hart queued for the console
now reads `lock_spin` at `console/src/lib.rs:136` instead of `print_wait`.
That is more specific, not noise, but it changes the `print_wait` counts.

**Coverage correction.** The first version instrumented only `SpinLock`.
`RwLock` has its own atomic loop (`ostd/src/sync/rwlock.rs`), and one of its
readers is on the interrupt path itself: `top_half::process` takes
`callbacks.read()` before any bottom-half marker runs. A hart parked there
would have read `target_lock=none` and looked like "not waiting for a lock".
The `RwLock` slow path now records the same site and address with the
`rw_spin` marker, so a report distinguishes the two kinds. With that, the
statement `target_lock=none` means "not on a `SpinLock`" -- and only after
the `RwLock` path was added can it begin to mean "not on a lock at all".
The `RwLock` here is reader-preferring (`try_read` sets no bit when it
fails), so an interrupt-path reader only spins while a writer *holds* it:
a hit means some CPU is holding the callback registry lock, not merely
queueing for it.

### 3.2 The third-party view: CLINT registers (pre-registered reading)

Every instrument so far observes the target from inside it. The CLINT/ACLINT
window is ordinary MMIO, so another hart can read the target's `MSIP` (an
interrupt was raised and not taken) and its `mtimecmp` (where its timer
deadline stands) whether or not the target is executing. The probe finds the
window in the device tree, maps it once read-only, and prints
`ASTERINAS_TIMER_MMIO base=… size=… node=…` at boot; the stall line then
carries

```
target_clint=hwid=<n> msip=<first>/<second> cmp_lag_ms=<i64> cmp_delta_us=<i64>
```

Two details that decide whether the reading means anything:

- **The registers are indexed by hardware hart id, not logical CPU id.** The
  mapping is `hw_cpu_id_mapping()`; reading by logical id would describe a
  different hart and look completely normal. `hwid=` is printed so every
  reading can be tied to a physical core.
- **`cmp_delta_us` is a two-sample measurement ~1 ms apart, busy-reading
  `mtime`** (never sleeping on a timer, which is the machinery under study).
  One sample cannot tell "the hart stopped re-arming its timer" from "the
  timer is fine".

Reading table, written down before the round:

| Reading | Conclusion |
|---|---|
| `msip=1` | the interrupt was raised and not taken: delivery or masking, not a lost firmware write |
| `msip=0`, `cmp_delta_us ≈ +1000` | the deadline was never moved: the target is not running its timer path at all (machine mode, or S-mode with IRQs off) |
| `msip=0`, `cmp_delta_us ≈ 0` or negative | the target re-armed its timer during the window: its execution is fine and the loss is on the delivery side |
| `cmp_delta_us` negative and large | the deadline was pushed into the future during the window: a normal re-arm |

The four are mutually exclusive and cover "target execution / timer re-arm /
interrupt delivery", so any value locates the failure to one of the three.

Because the window may turn out to be unmappable (the firmware can take MMIO
away from S-mode), the same question is also answered from inside the
kernel: `target_settimer_age_us=` is the time since the target last entered
the SBI call that arms its timer. A small age (within one tick period) means
the target is still running a timer path -- periodic or one-shot; an age near
a second and growing means it is not. It does **not** separate a long
IRQ-off section from a firmware call that never returned, so it answers
"still scheduling timer work?", never "in machine mode?" -- that needs
`msip`/`cmp_delta_us` from the outside view. Reporting both is deliberate:
two independent methods agreeing is itself evidence, and if the MMIO is
blocked the internal one still exists.

**Result: the outside view is closed (b118).** With the Megrez tree's lack
of a CLINT node worked around by the vendor constants, the probe mapped the
window, printed the address line, and the very first supervisor load --
`msip` of the boot hart -- raised `LoadFault`. The firmware's PMP keeps the
ACLINT window machine-mode only. `target_clint=` can therefore never carry a
reading on this board; the field is kept, the read is opt-in behind
`asterinas.clint_read=1`, and the default value is `clint-off`.

That experiment also produced a lasting rule: **a diagnostic that touches
hardware must not be on the stall-report path unless it is gated.** The
first version read the CLINT unconditionally when building the stall line,
so the load fault panicked the sender at the one instant the evidence was
about to be printed -- the round ended with a `LoadFault` and no stall line,
and the death time was our own instrument's, not the board's. The gate is
now checked before the window is touched (`clint-off` / `clint-unmapped` /
`clint-readfail` are three different answers).

## 4. Instrumentation added (all diagnostic, default off)

**These are not leftover debugging.** Each one answered a specific question
that nothing else could, and together they are the evidence chain that led
to the fix. Keep them; they are also the regression line for anything that
touches interrupts.

| Instrument | The question it answers | Where to look |
|---|---|---|
| `storms=` in the heartbeat | has a stuck source been masked since boot (even if its line was lost in the log)? | heartbeat |
| `ASTERINAS_IRQ_STORM line=<n> served=<n> action=masked` | which source stormed, and was the bound hit? | boot log, at the storm |
| `ASTERINAS_IRQ_ALLOC num=<n> site=<file>:<line>` | which driver owns software IRQ `n`? | boot log |
| `ASTERINAS_IRQ_MAP num=<n> plic_source=<m>` | which PLIC hardware source does software IRQ `n` correspond to? | boot log |
| `target_activity=plic_claim` + `age_us` | is the target inside the external-IRQ claim loop, and is it still executing? (age of a few microseconds = alive and spinning) | stall report |
| `target_last_ext_irq=` / `target_ext_irqs=` | which external interrupt, and at what rate? | stall report |
| `target_irq_kind=` | which interrupt did the target last start handling? | stall report |
| `target_last_ipi_age_us=` | when did the target last enter the IPI path? | stall report |
| `target_settimer_age_us=` | is the target still arming its timer at all? | stall report |
| `target_lock=` / `target_lock_addr=` / `act=lock_spin` / `rw_spin` | is a hart (or two) parked on a `SpinLock` or `RwLock`, and which instance? | stall report |
| `target_bh_depth=` | how deep in bottom-half nesting? (rules out interrupt storms inside the dispatcher) | stall report |
| `target_clint=` (`clint-off`/`clint-unmapped`/`clint-nohwid`/`clint-readfail`) | why is the CLINT view unavailable? (on Megrez: the firmware's PMP blocks S-mode reads entirely) | stall report |
| `ASTERINAS_TIMER_MMIO status=… source=<dtb\|eic7700-const>` | where is the ACLINT window and where did the address come from? | boot log |
| `ASTERINAS_UART_RX ... irq=<n>` | which software IRQ the console UART owns | boot log |
| `asterinas.console_rx=off` | run without the UART receive interrupt (one candidate was ruled out this way) | cmdline |
| `asterinas.clint_read=1` | opt in to the CLINT read that faults on this board (evidence, not routine) | cmdline |
| `uw=`/`fw=`/`io=`/`halt=`/`sepc=`/`line_bytes=` | console cost, longest IRQ-off window with its site, idle-loop entries | heartbeat |

Kernel command line: `asterinas.tlb_no_remote_flush`, `asterinas.sbi_ipi_serialize`,
`asterinas.ipi_no_resend`, `asterinas.watchdog_heartbeat_every`,
`asterinas.watchdog_stop_kick_after`, `asterinas.wfi_enable_first`,
`asterinas.telemetry_hz`, `asterinas.timer_hz`, plus the existing IPI stress
knobs.

Heartbeat: `act=` per-CPU activity (`idle`/`print_wait`/`print_write`/`early`/`wait`),
`uw=`/`fw=` write-duration statistics for the kernel UART and the firmware
console, `io=<max>,<src>,<done>` longest IRQ-off window with its call site,
`halt=` per-CPU idle-loop entries, `sepc=`, `line_bytes=`, `uptime_ms=`.

Stall report: waiter and target activity, both harts' trap counts since the
wait began, split into all traps and software-interrupt traps, `all_activity`,
the target's current IRQ-off window with its file:line, `target_wait_ms=`,
`target_bh_depth=`, `target_lock=`/`target_lock_addr=` for a hart that is
spinning for a lock, `target_clint=` (the CLINT view: `clint-unmapped`,
`clint-nohwid`, `clint-readfail`, or `hwid=… idx=… msip=a/b cmp_lag_ms=…
cmp_delta_us=…`), and `target_settimer_age_us=`.

Boot line: `ASTERINAS_TIMER_MMIO status=ok source=<dtb|eic7700-const>
mswi_base=… mswi_first_hartid=… mtimecmp_base=… mtime_base=time-csr n=…
mswi_order=[…] mtimer_order=[…]`, then `status=read self_hwid=… msip=…
mtimecmp=…`, or `status=skip reason=<no-mswi-reg|no-mtimer-reg|
mmio-unavailable> source=… msip_base=… mtimer_base=…`. Its absence means the
probe never ran.

The Megrez tree has no CLINT node at all (the firmware's platform code
hard-codes the window), so the board falls back to the vendor constants
`MSIP[hartid] = 0x0200_0000 + 4×hartid`, `MTIMECMP[i] = 0x0200_4000 + 8×i`
(`eic770x/platform.c`, `first_hartid = 0`). `source=` says which path was
taken, so "the tree did not describe the window" is never confused with "the
window is inaccessible". The probe also reads the window once at boot on
purpose: if the firmware's PMP keeps it machine-mode-only, the load faults
and the kernel panics at boot, immediately, instead of a stall report that
can never read anything.

## 5. Harness lessons that produced false failures

- A dropped first character turned `booti` into `ooti`; the kernel never
  started and the run looked like an instant death. Boot commands are now
  verified by which output appears, not by the echo.
- `deploy_img.py` matched a previous transfer's `Bytes transferred` line.
- A boot retry loop ran past the watchdog window and reset the board mid-setup.
  Armed-watchdog time is budgeted explicitly now.
- The idle path forgets its IRQ-off guard (`halt_cpu` uses `mem::forget`), so
  the first "1-second IRQ-off window" reading was the tracker, not the board.

### 5.1 Eight verification-layer traps, all found in one night

Every one of these produced a **confident, wrong** "the thing under test is
fine" (or "is broken"). None was a defect in the thing under test: each was
the measuring apparatus reporting about itself. The cure is the same in every
case -- give the apparatus a positive control that proves it took effect.

| Trap | What it looked like | Positive control |
|---|---|---|
| `make ktest-local ARCH=riscv64` | tests "passed"; the makefile keys off `TARGET_ARCH`, so it silently ran x86_64 | `grep -o 'qemu-system-[a-z0-9_]*' log` must say `qemu-system-riscv64` |
| `CARGO_OSDK_TEST_ARGS="--release ..."` | "release also fails"; the override dropped `--scheme riscv`, so it was still a dev build | `Finished \`release\` profile` in the build log |
| `make ktest-local` exit status | `$? == 0` while a `test result: FAILED` line was present | parse `test result:` lines, never the exit code |
| `KTEST_FILTER=ext2` | "ext2 tests pass": `ok. 0 passed; 254 filtered out` -- the filter is a suffix match, nothing ran | assert the expected test *names* appear, and `0 filtered out` |
| crash gate on a kernel without `asterinas.ext2_sync_hold_ms` | "5/5 green" in churn mode; the parameter was ignored, the kill window never widened, the interesting path was never hit | grep the kernel for the hook, or assert the guest prints that it is holding |
| crash gate that runs but kills outside the widened window | the same green, with the hook present but the stop point in the wrong place (or too short to land on) | a **control kernel with the bug**: the gate must show a non-zero dangling count there, or its green on the fixed kernel means nothing |
| crash gate whose kill depends on reading a line | both the fixed kernel and the buggy control showed `dangling=0`; the gate blocked in `readline()` until the widened window closed, so every kill landed after a complete sync | kill on a **timer**, with a drain thread reading the console; and a tell-tale: a run killed inside the window cannot print a second round marker |
| "the repository is rustfmt-clean, so formatting churn is noise" | a `cargo fmt --all` run touched 28 files nobody had edited; the instinct "revert them" rested on an unverified premise | verify the premise: `git worktree add ~/x HEAD && cargo fmt --all --check` -- it flagged 68 files, i.e. HEAD was never formatted; a repo-wide `style:` pass is a separate decision |

Related apparatus failures outside the tooling: the serial bridge's reader
thread died twice (once from a full `/tmp` tmpfs, once for an unknown reason)
and the log simply stopped -- which reads exactly like a dead board until the
bridge process and its thread count are checked; and a line stamped by the
file offset it *starts* at attributes the silence that follows it to the wrong
line, which is what produced the phantom "reset 0.1 s after the stall" (§1.6).

### 5.2 The ninth trap: `bootcmd` still loads the old kernel

The board's SPI `bootcmd` loads `kernel-rel.img`, not the image we deploy:

```
ext4load mmc 1:3 0x80200000 kernel-rel.img     # old kernel, by default
ext4load mmc 1:1 0x88300000 stage1-daily.cpio
ext4load mmc 1:1 0x88000000 megrez-daily.dtb
booti 0x80200000 0x88300000:${initrd_size} 0x88000000
```

A fixed build only runs when U-Boot is interrupted and `kernel-fix.img` is
loaded by hand (§1.8's 3/3 runs were exactly that: RAM-only boots). **Every
`reboot`, every watchdog reset, and every power cycle silently returns to the
old kernel**, so "we deployed the fix and the board still misbehaves" is
indistinguishable, from the serial console alone, from a failed fix.

Positive control: the diagnostic markers only the fixed build prints
(`ASTERINAS_WATCHDOG_HEARTBEAT`, `ASTERINAS_TIMER_MODE`, `ASTERINAS_UART_RX`)
are absent -- zero occurrences in the whole boot segment. Count them before
concluding anything about a deployed kernel; also `ext4ls mmc 1:3` first so a
missing `kernel-fix.img` cannot be mistaken for a broken fix.

This trap caused a further incident: with the DW watchdog deliberately
disabled at U-Boot (to widen a diagnostic window) and the old kernel booted
from `bootcmd`, the machine hung with no self-recovery path and needed a
physical power cycle. The watchdog is not optional; see
`board-never-boot-without-watchdog` in the project memory.

