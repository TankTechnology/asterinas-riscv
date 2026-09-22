# Deterministic DW-APB receive boundary tests

The user approved minimal, directed tests instead of prolonged load. This work
tests the existing receive protocol; it does not presume a hardware bug or claim
to reproduce the physical playback stall.

- [x] Audit existing tests: mask preparation and bounded console delivery are
  tested separately; the deferred receive/rearm function has no direct coverage.
- [x] Establish the 80-test UART baseline in a clean worktree with the persistent
  Docker launcher and shared dependency caches.
- [x] Make the existing deferred function generic over register access and accept
  its reschedule operation as a closure. Preserve its control flow and production
  Taskless scheduling. Keep all hardware modeling inside the existing test module.
- [x] Add two deterministic tests through the real top half, deferred handler,
  and console receive callback. A small FIFO/IER model injects a byte just after
  an empty LSR observation; pending receive data is surfaced as a timeout cause.
  Check exact byte delivery, retained vendor IER bits, rearm, and bounded retry.
- [x] Run the UART suite. Temporarily remove rearm and reschedule separately;
  require the corresponding tests to fail, then restore the source and rerun.
- [x] Review the change and record results, limits and remote-main status.

Validation: from `kernel/comps/uart` inside the isolated persistent container,
`CARGO_NET_OFFLINE=true SMP=4 CONSOLE=ttyS0 timeout -k 5 180 cargo osdk test
--release --target-arch riscv64 --scheme riscv --features ostd/riscv_sv39_mode`.
The timeout bounds build plus execution; the new tests contain no waits or sleeps.

The model covers software handling of pending RX/character-timeout events, not
physical FIFO timing, interrupt delivery by the PLIC, or Taskless execution timing.
No physical reboot or remote main push is part of this task.
