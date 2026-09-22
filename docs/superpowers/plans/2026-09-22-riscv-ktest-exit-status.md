# Preserve RISC-V guest test failures at the host

The user approved fixing the reproduced false-success result with a minimal
regression. Reuse the existing isolated worktree and persistent container.

- [x] Trace the cause: the QEMU virt DT describes syscon-poweroff with the fixed
  FINISHER_PASS value; OpenSBI's syscon reset driver ignores reset reason.
- [x] Add a small OSDK integration test that builds a temporary two-test library
  and checks actual guest summaries plus CLI exit 1 for failure and 0 for success.
  QEMU has one CPU, 256 MiB, no disks/network. Observe failure before the fix.
- [x] Reserve the FDT-described `sifive,test1` MMIO region in OSTD before general
  I/O allocation. On QEMU, send success/failure directly to the finisher; retain
  SBI fallback when that device is absent. Reuse the unambiguous failure status
  already recognized by OSDK. Do not parse serial text as the CLI verdict.
- [x] Run the end-to-end regression and UART suite, check formatting, review,
  and save the actual result/exit status evidence. Keep changes on a local branch.

The integration test is explicitly ignored by the generic host suite because it
needs the RISC-V QEMU/toolchain. Invoke it with `cargo test --manifest-path
osdk/Cargo.toml --test integration riscv_guest_test_result_reaches_host --
--ignored --nocapture`, inside the project container with `OSDK_LOCAL_DEV=1`.
This takes two short guest boots, not a stress interval.
