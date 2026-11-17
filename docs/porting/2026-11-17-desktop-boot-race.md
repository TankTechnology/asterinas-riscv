# The rebase kernel's flaky desktop boot on Megrez (2026-11-17)

Status: **resolved 2026-11-17**. The three fixes in `06ea8220e` are kept and
nothing is pushed. The repeated fault at `0x3ffeffe27e` is the EIC7700 faulting
on a store to a writable entry whose DIRTY bit is clear: the handler's
"already permitted" branch only flushed and never recorded the access, so the
store could never retire. See `2026-11-17-dirty-bit-fault-loop.md` for the
mechanism, the deterministic reproduction and the fix. The sections below are
kept as the record of how the investigation went, including the two
conclusions that turned out to be wrong (the IPI "storm" is ordinary boot-time
TLB shootdown traffic printed at `loglevel=debug`, and the failures were
observed while the rootfs was corrupt and full, which is also when the desktop
happened to touch a page in the load-then-store order).

## What flips between success and failure

The desktop boot is *not* deterministic. With the same kernel, rootfs and
command line (`asterinas.reboot_after=900`, the deployed value) three outcomes
appear across cold boots:

| Outcome | Evidence | Rate observed |
| --- | --- | --- |
| Full desktop | `ASTERINAS_DESKTOP_BOOT_READY`, Firefox renders Baidu | 1 run (traced build) |
| `HANG_AFTER_EXEC` | stage1 completes, `execve done pid=1`, then the same user address faults forever | 3/3 (clean build), 1 run (traced) |
| `EARLY_RESET` | kernel reaches `[kernel] running /init`, starts loading the init ELF, then the SoC resets with no panic text | 6/6 (later traced builds) |

The trace instrument itself shifts the distribution, so the two failures are
timing-sensitive views of the same instability, not three unrelated bugs.

## HANG_AFTER_EXEC: one address faults forever

Trace (early_println, hardware timestamp):

```
[1.056] BOOTTRACE execve begin pid=1
[1.072] BOOTTRACE execve done  pid=1        <- systemd installed
[1.084] BOOTTRACE pf#200 handled=true PageFaultInfo { address: 274861122174, required_perms: RW }
... 145 000+ identical faults by t=4.4 s
```

`274861122174` is `0x3ffeffe27e`; the page is `0x3ffeffe000`. Every fault is
reported `handled=true`, yet the hardware faults again on the next retry, so
the instruction never retires and the process makes no further syscall.

Things this is *not*: it is not the mprotect ACCESSED-bit loss (fixed in
`06ea8220e`; that fault storm started right after an `mprotect`), and not the
COW local-flush gap (also fixed; the COW-path markers never printed in the
failing run). The failing address is reached before the COW branch prints, and
the perms check shows `READ|EXEC|MAY_READ|MAY_WRITE|MAY_EXEC`, `forced=true`
— the kernel's own forced write while initializing the new image, not a user
store.

## EARLY_RESET: an inter-processor call that never completes

Trace:

```
BOOTTRACE init-create begin
DEBUG: ELF segment: virt_range = 0x10000..0x83f5c ...
DEBUG: self.perms READ|EXEC|MAY_READ|MAY_WRITE|MAY_EXEC, required WRITE, addr 0x83000, forced true
DEBUG: ELF segment: virt_range = 0x84028..0x97458 ...
DEBUG: Successfully sent IPI to hart 0
DEBUG: Performing inter-processor call to 0xffffffff80651524 on CPU 1/2/3 (repeating)
<SoC reset, no panic output>
```

An inter-processor call is issued while the init ELF is being loaded, and the
board resets a fraction of a second later. This is the same shape as the
`loglevel=debug` boot that livelocked in an IPI storm earlier in the port
(observed during this session, not written up before): repeated
`Performing inter-processor call` on every hart with no forward progress.
With the hardware watchdog armed (~10 s window) and the kernel no longer
kicking it (the kick runs from a timer callback), a stuck cross-CPU call
becomes a hard reset instead of a hang.

## Instrumentation pitfalls found on the way (do not repeat)

* `ostd::info!` (logger path) must not be called from the page-fault or syscall
  path: it livelocks/resets in the same way the `loglevel=debug` boots did.
  `ostd::early_println!` is safe there.
* `crate::current!()` panics in the early init-creation context (before the
  task is registered as a POSIX thread). A trace that calls it resets the
  board by itself; that is what the first `EARLY_RESET` builds were actually
  doing.

## The failure narrows to a cross-CPU call storm while the init ELF loads

Instrumenting the IPI path (`inter_processor_call` sender and
`do_inter_processor_call`) shows CPU 0 issuing the **same** callback
(`0xffffffff8064c92c`, unchanged in every send) to CPUs 1/2/3 dozens of times
within a few milliseconds while `create_init_task` loads the init image, and
the boot never moves past it: the targets all run the callback (the handler's
enter is logged on each), so it is a storm of state changes, not an
unanswered IPI. This is the callback a TLB flush dispatch sends, so the loader
is redoing page-table work in a tight loop.

The same boot in a different build shows the other face of the instability: a
repeated user fault at one address. Both are consistent with one defect in the
fast path that maps/remaps pages for a process that is still being built
(exec/init creation), where the board's translation caches make the retry
never observe the new mapping.

## Where to look next

* The IPI path itself: `ostd/src/arch/riscv/irq/ipi.rs` and
  `ostd/src/smp.rs` (`do_inter_processor_call`), plus `TlbFlusher`'s
  `inter_processor_call`/`pending_ipis.wait()`. The stuck call happens while a
  freshly created address space is being populated, i.e. during a TLB
  shootdown for a page table that is still being built.
* Whether the reset is the watchdog (check `asterinas.watchdog_drill` and the
  handoff kick) or a hard fault: boot with the watchdog left unarmed, so a
  stuck boot shows as a hang instead of a reset, and read the PC with a
  serial SysRq or a debugger.
* The `0x3ffeffe27e` fault: dump the PTE and the `is_forced` path of
  `handle_single_page_fault` for the init process, without calling
  `current!()`.

## Recipe used

`/tmp/board-net/boot_once.py` boots the kernel on `/root/kernel-desktop.img`
over the serial console, classifies the outcome, and recovers with the serial
SysRq (`!SYSRQ b`, verified working on a hung board) so a cycle costs ~3 min.
`/tmp/runs/*.log` keeps each boot's window.
