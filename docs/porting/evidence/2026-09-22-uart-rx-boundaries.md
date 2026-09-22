# Deterministic UART receive boundary validation

Two directed DW-APB tests pass without reproducing the physical playback stall.
Each runs a fixed sequence through the real interrupt preparation, deferred
receive function and console callback. Neither uses sleeps, retries until lucky,
background traffic or a duration-based load.

## Change and coverage

Baseline: clean commit `3aa196979a4330b78a2453ddb567e24b4d85a5a1`.
Worktree: `.worktrees/uart-rx-boundaries`, branch `codex/uart-rx-boundaries`.

`process_deferred_rx` now accepts the existing register-access abstraction and
a reschedule closure, allowing the tests to invoke the production control flow.
The production closure still calls the same urgent Taskless scheduling operation
at the same point. No RX algorithm or timing change was made.

- `dw_apb_deferred_rx_retries_at_the_budget_without_unmasking`: deliver 64 bytes
  from a 65-byte pending stream, require one deferred retry with RX still masked,
  then deliver the last byte and rearm. Verify exact callback byte order, no loss
  or duplication, and preservation of the vendor IER bit.
- `dw_apb_deferred_rx_delivers_input_arriving_before_rearm`: inject a second byte
  immediately after an empty LSR observation while RX is masked. Require rearm,
  a subsequent receive interrupt, and delivery of both bytes in order.

The test register model represents pending input, RX enable, and character
timeout notification. It does not model physical FIFO capacity/trigger timing,
PLIC delivery or concurrent Taskless execution. Thus these results establish
software handling of the specified events, not physical-board stability.

## Validation

All build/execution work used the persistent Docker launcher. The isolated
container has its own writable build directory and cached dependencies. The
standard QEMU configuration requires disk paths; four disposable 16 MiB sparse
placeholder images satisfy those paths. UART ktests do not use their filesystems.
No physical device or RockOS state was changed.

From the isolated container's `/root/asterinas/kernel/comps/uart`:

```sh
CARGO_NET_OFFLINE=true SMP=4 CONSOLE=ttyS0 timeout -k 5 180 cargo osdk test \
  --release --target-arch riscv64 --scheme riscv --features ostd/riscv_sv39_mode
```

| Run | Actual guest result |
|---|---|
| Unmodified baseline | 80 passed, 0 failed, 0 filtered; runner completed |
| First run with both new tests | 82 passed, 0 failed, 0 filtered; runner completed |
| Temporarily omit RX rearm, run rearm test | 0 passed, 1 failed, 81 filtered; IER 128 instead of 129 |
| Temporarily omit reschedule, run budget test | 0 passed, 1 failed, 81 filtered; retry count 0 instead of 1 |
| Restore final source, full UART suite | 82 passed, 0 failed, 0 filtered; runner completed |

The negative runs use the **entire test function name** as the positional filter
to `cargo osdk test`. A partial-name attempt selected zero tests (82 filtered)
and was excluded; the runner matches path components, not arbitrary substrings.
The 180-second limit covers build plus execution, not a requested test duration.

`rustfmt --check --edition 2024` on the changed Rust file and `git diff --check`
passed. Independent read-only review of the final source found no actionable
issues. Temporary mutations were removed and the restored source was verified
byte-for-byte against the reviewed snapshot before the final run.

The adjacent `2026-09-22-uart-rx-boundaries/` directory preserves four decisive
logs and their SHA-256 manifest. Setup failures (missing local fixture paths,
then an overly broad test filter) are not counted as validation results. Full
working logs and source snapshots remain in `target/uart-rx-boundaries-20260922/`.

## Additional finding: failed guest tests can return host status zero

Both deliberate failures produced explicit guest `FAILED` summaries while the
launcher/OSDK host command returned status 0. The test kernel does call
`poweroff(ExitCode::Failure)`; RISC-V maps that to SBI shutdown with a system
failure reason. OSDK treats process status 0 as success. The exact loss of the
failure indication between SBI/QEMU and OSDK needs its own small regression.

This is a reproduced false-success risk in the test execution path, not a UART
failure. Every verdict above was checked against actual guest counts and test
names; no verdict relies on the host exit status alone. Fixing failure-status
propagation is a more useful next step than increasing load duration.

## Remote-main status

A fresh `git fetch origin main` found remote main at `d60a39ba4`.
The original working branch `dns-shim-isolated-console-fix` at `ac1dd9888` is
10 commits ahead and 0 behind it. That includes the RISC-V trap `gp` fix and
its validation records. The earlier migration/usercopy regression and QEMU
stability report were still uncommitted in the original workspace.

This UART change is kept on its isolated local branch. No remote push or merge
to main was performed during this task.
