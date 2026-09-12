# Syscall compatibility safety plan

The requested scope is kernel correctness, straightforward missing interfaces,
and small regression tests before further desktop experiments.

## Evidence and bounded scope

The retained physical `network-stack-integration/physical/firefox-final/`
`megrez-gmac.serial.log` contains 120 unimplemented syscall 272 (`kcmp`)
warnings and one syscall 428 (`open_tree`) warning. These are RISC-V numbers.
The warnings contain neither caller identity nor arguments; frequency alone
does not establish Firefox attribution or startup cost.

A wider inventory of retained physical logs additionally finds syscall 213
(`readahead`) and 60 (`quotactl`). Aggregating repeated experiments is only
an inventory, not an independent frequency or performance measurement.

`kcmp` requires access checks against both target threads and namespace-aware
lookup, as well as resource identity comparisons. The current PID namespace
helper exposes processes, not a general virtual-TID lookup. Do not introduce
an unchecked global-TID shortcut or pretend unsupported comparison kinds work.
`open_tree` needs mount-handle semantics and is not a small warning cleanup.
For `readahead`, a detached `inode.page_cache()` shortcut is also unsafe:
the page cache requires the filesystem's truncate/invalidation locking, and
overlayfs's cache accessor can trigger copy-up. A real implementation needs a
filesystem-level prefetch hook and overlay forwarding. Do not reuse a fault
helper that bypasses batching under profiling or rejects anonymous caches.

The audit additionally found that the existing `rseq` handler reports success
without restart-on-preemption/signal handling, fixes CPU IDs to zero, and writes
four signature bytes at offset 32 after accepting a 32-byte region. The
signature is not a field following the registration area. This memory-safety
and capability-advertisement defect takes precedence over adding `kcmp`.

## Rseq repair

- [x] Add a small userspace regression that detects writes beyond the supplied
  region on the frozen pre-fix release image; preserve the behavioral failure.
  `target/rseq-safety/before-final/serial.log` reports successful registration,
  `canary_intact=0`, and the assertion failure (`RSEQ_RESULT=134`).
- [ ] Until the full restart protocol is implemented, return `ENOSYS` without
  touching userspace memory or retaining a registration. Remove the obsolete
  per-thread registration and exit-time user-memory write.
- [ ] Test the fallback explicitly, including unchanged memory, invalid
  arguments and libc/thread startup. Distinguish Linux supported/registered
  behavior from Asterinas's intentionally unsupported interface.
- [ ] Review the scoped diff, build offline after the fault-window build lane
  is released, and run the regression in QEMU.
- [ ] Keep this change separate from the fault-window performance comparison;
  validate the combined candidate with desktop/libc startup afterwards.

## References

- Linux UAPI: <https://raw.githubusercontent.com/torvalds/linux/v6.12/include/uapi/linux/rseq.h>
- Linux kcmp contract: <https://man7.org/linux/man-pages/man2/kcmp.2.html>

Implementing full rseq, mount handles, or namespace-aware kcmp is not claimed
by this repair. No speedup is attributed to these changes without measurement.
