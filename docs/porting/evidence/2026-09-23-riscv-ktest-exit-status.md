# RISC-V QEMU ktest failure propagation

The minimal end-to-end regression now observes CLI status **1** for a failed
guest test and **0** for a successful guest test. Before the fix, the same failed
guest test returned status 0. Validation uses one CPU, 256 MiB, no disks/network,
and two finite guest test invocations; it does not depend on prolonged load.

## Cause

The observed firmware identifies its shutdown device as `syscon-poweroff`.
[QEMU v10.0.0's virt device tree](https://github.com/qemu/qemu/blob/v10.0.0/hw/riscv/virt.c)
sets that node's value to `FINISHER_PASS`.
[OpenSBI's syscon reset driver](https://github.com/riscv-software-src/opensbi/blob/master/lib/utils/reset/fdt_reset_syscon.c)
writes the configured value without using the SBI reset reason. Thus OSTD's
existing `Shutdown/SystemFailure` request loses its failure indication there.

[QEMU's SiFive test device](https://github.com/qemu/qemu/blob/v10.0.0/hw/misc/sifive_test.c)
accepts an exit status in the upper half of its 32-bit command. The firmware boot
log also confirms S-mode read/write permission for this device's region.

## Change

OSTD discovers an enabled `sifive,test1` node, checks its register alignment,
minimum size and range overflow, and reserves the device as sensitive MMIO before
publishing the general I/O allocator. No fixed physical address is assumed.

Poweroff writes the finisher directly: `0x5555` for success, and
`(65 << 16) | 0x3333` for failure. Status 65 is already the guest-failure status
recognized by OSDK for x86's debug-exit device, avoiding confusion with QEMU's
own startup error status 1. The host maps it to CLI status 1. After issuing the
command, the current CPU halts with local interrupts disabled so a subsequent
SBI shutdown cannot replace failure with success.

Platforms without the compatible device retain the existing SBI path. Restart
handling is unchanged. The new unsafe register access is confined to OSTD and
documented; kernel crates cannot acquire the reserved sensitive region.

## Regression and observations

`osdk/tests/commands/test.rs::riscv_guest_test_result_reaches_host` creates a
temporary OSTD library containing one deliberately failing test and one passing
test. It executes each by exact name and asserts both the actual guest counts
and the host status. The temporary project is cleaned up automatically.

| Run | Result |
|---|---|
| Regression before fix | Guest: 0 passed / 1 failed / 1 filtered; CLI returned 0 instead of required 1; host regression failed |
| Regression after fix | Both cases passed their assertions: failed guest → CLI 1; successful guest → CLI 0 |
| Existing UART suite with fixed OSTD | 82 passed / 0 failed / 0 filtered; runner completed |
| Formatting and patch whitespace | Passed |

The final regression took 12.63 seconds including building its temporary guest after
the fix. It is opt-in in the generic OSDK host suite because it requires RISC-V
QEMU and the cross-compilation toolchain:

```sh
tools/docker/run_dev_container.sh \
  --workspace /mnt/shared/xaj/Program/asterinas/.worktrees/uart-rx-boundaries -- \
  bash -c 'OSDK_LOCAL_DEV=1 CARGO_NET_OFFLINE=true cargo test \
    --manifest-path osdk/Cargo.toml --test integration \
    riscv_guest_test_result_reaches_host -- --ignored --nocapture'
```

Each invocation is bounded by GNU `timeout`, which terminates the entire process
group after 120 seconds (with a five-second forced-kill grace period). Review
identified that the initial assert_cmd timeout killed only OSDK and could leave
QEMU holding output pipes open; that was corrected and the final regression
passed again. Follow-up review found no remaining actionable issues.

All builds used the existing persistent container. The initial fixture had an
incorrect macro import and failed compilation; that setup attempt is excluded
from the red/green result above. `red-guest.log` is the actual pre-fix behavioral
failure. Readable logs normalize line endings and trailing whitespace; `raw-logs.tar.gz`
preserves the original bytes and hashes. Logs and SHA-256 hashes are in the adjacent
`2026-09-23-riscv-ktest-exit-status/` directory. Working logs remain in the main
workspace's `target/riscv-ktest-exit-status-20260922/` directory.

This qualifies direct QEMU virt poweroff through the test finisher. It does not
claim failure-status propagation on physical SBI-only platforms, nor qualify
OSDK configurations that keep QEMU alive after shutdown and later issue monitor
`quit`. No development-board reboot, network change or remote push occurred.
The work is on local branch `codex/riscv-ktest-exit-status`, based on the previous
UART test commit `8be111679`.
