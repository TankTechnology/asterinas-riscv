# Kernel log interfaces: local implementation and evidence

Date: 2026-09-08.
Worktree: `codex/megrez-physical-graphics-current-main`, based on
`cc66b9d0b2b90c046abd7ffa6db2fb0e86a9e2d0`, with pre-existing local changes preserved.
This milestone implements local kernel logging interfaces; it does not deploy to the board.
No commits, remote PR changes, dependency installations, or Docker/cache removals were performed.

## Provenance

The design borrows lessons from TankTechnology's
[#2265](https://github.com/asterinas/asterinas/pull/2265)
at `2830167d362082dfac37900c9d38a9f355f1cce5`
and its successor [#3288](https://github.com/asterinas/asterinas/pull/3288)
at `706b90675ab0f884e8fd232ec41be5284f343ee8`.
It is not a cherry-pick of either implementation.
The older byte-stream approach was not reused because independently positioned
`/dev/kmsg` readers need complete records, sequence numbers and overwrite detection.

The recorded review history is more specific than rejection of AI assistance:
[the maintainer's May 12 comment](https://github.com/asterinas/asterinas/pull/2265#issuecomment-4426643998)
allowed AI assistance but requested thorough self-review and recommended the review skill.
[The author closed #2265 on June 14](https://github.com/asterinas/asterinas/pull/2265#issuecomment-4701219615)
because #3288 continued the work.
At inspection, #2265 was closed unmerged and #3288 was open.

Compatibility references are the
[Linux device ABI](https://www.kernel.org/doc/Documentation/ABI/testing/dev-kmsg),
[syslog manual](https://man7.org/linux/man-pages/man2/syslog.2.html),
and [Linux v6.16 printk implementation](https://github.com/torvalds/linux/blob/v6.16/kernel/printk/printk.c).

## Implemented scope

- `aster-logger` owns a static 256-record ring, retaining up to 1024 message bytes per record.
  Formatting original arguments happens once, before the IRQ-safe store lock.
  Capture and console filtering are independent; console output and reader wakeups do not run under that lock.
- `/dev/kmsg` has per-open cursors, shared cursors through `dup`, whole escaped records,
  nonblocking and interruptible blocking reads, polling, special seeks and `EPIPE` overwrite repair.
  User writes use a non-kernel facility and follow console filtering.
- `syslog(2)` actions 0–10 share the record store while maintaining an independent destructive cursor.
  Clearing advances a marker rather than deleting records needed by other readers.
  Signal restart and failed user-copy clear boundaries have dedicated regressions.
- `/proc/sys/kernel/dmesg_restrict` defaults to `1`.
  Privilege checks use the initial user namespace and the existing capability/LSM path.
  The historical `CAP_SYS_ADMIN` fallback is deliberately retained;
  Linux v6.16 instead requires `CAP_SYSLOG` alone.
- The process regression suite includes `syslog`.
  `make test_klog_store` runs the actual pure record-store tests without an OS boot
  and is also a dependency of `make test`.

This is the kernel `syslog(2)`/`klogctl` interface, not a new `/dev/log` socket or `syslogd` service.

## Evidence and negative controls

All artifacts below are relative to `target/klog-20260908/` in this worktree.
No result is a physical-board or Firefox result.

| Check | Result |
| --- | --- |
| Pure Rust store tests | 12 passed; includes 10,000 ring rollovers and single evaluation of stateful formatting arguments |
| Gate validation tests | 12 passed; rejects missing/conflicting markers, misplaced evidence, failed console restore and fatal output after success |
| Final incremental RISC-V build | Passed, 9.90 seconds; 12 existing kernel warnings, no warning in the new logger/adapters |
| SMP4 micro guest | 65 C assertions, kernel/console capture probe, BusyBox dmesg, util-linux dmesg and two follow acknowledgments passed; 12.401 seconds |
| SMP1 micro guest | Same checks passed; 12.702 seconds |
| Independent reviews | Store and adapters passed specification and maintainability/development/security review after minor fixes |

The two final boots used the same frozen kernel and fixture and were launched concurrently.
Their elapsed times are observations of this small QEMU experiment, not a kernel-overhead benchmark.
The probes use a roughly 13 MiB initramfs and no guest networking.
`result.json` records command, input SHA-256 values, elapsed time and verdict;
`serial.log` preserves the full transcript, not just successful lines.
Input hashes are checked again after the guest exits.

Final artifacts:

- `final-smp4/result.json`, `final-smp4/serial.log`.
- `final-smp1/result.json`, `final-smp1/serial.log`.
- `kernel-final.Image`: `7ab87e4db7f268bbd71b435ba3abe5c8fe487e916a0c456fa23b2ac501b10e6c`.
- `micro-v4.cpio`: `2012e997082bfa79ed07f1a606c44642cf638c2c64128a2890bd1929c1d6357e`.
- `build-final.log`, `store-review.md`, `abi-review.md`.
- `final-validator-tests.log`, `final-transcript-revalidation.json`:
  the final tightened validator rechecked both retained transcripts successfully,
  without another guest boot for parser-only changes.
- `source-SHA256SUMS` identifies the final source files, including later documentation-only refinements.

The baseline kernel from the preceding diagnostic milestone returned `ENOSYS`
for the probe and BusyBox dmesg (`baseline-valid.serial.log`).
An earlier `baseline.serial.log` is an invalid harness attempt:
it tried to mount a devtmpfs that this kernel does not provide;
the valid fixture uses the kernel's already populated `/dev`.

The first implementation kernel exposed two genuine defects under the newer tests:
one failed READ_CLEAR fault-boundary assertion and one failed SA_RESTART assertion.
`refinement-red.serial.log` and `final-red/serial.log` retain these failures.
The same final fixture also shows that this older kernel did not print user-written
records when the console was enabled.
The final kernel fixes all three behaviors.

Two failed dmesg-follow attempts were fixture defects, not evidence of a kernel deadlock:
`--raw` and `--color=never` are mutually exclusive in util-linux,
and a fixed 32-read loop was too short to drain 256 retained records.
With a five-second deadline and progress counters, the frozen first implementation
reached its first injected marker at read 206 and then acknowledged another marker.
The maintained gate requires two acknowledgments from the same still-running child.

The kernel/console probe triggers a real unimplemented-syscall warning,
checks its kernel facility/severity through `/dev/kmsg`,
and verifies that it was not printed while the console was disabled.
It separately validates hidden user records, enabled output and restored output.
It finishes at Linux's CONSOLE_OFF threshold (emergency-only, level 1),
not the initial boot-off threshold 0, because the console-level action discards the saved level.

## Repeating the small experiment

Use the persistent development container and cached inputs.
From the host:

```sh
KLOG_WORKTREE=/home/ubuntu/.config/superpowers/worktrees/asterinas/megrez-physical-graphics-current-main
klog_dev() {
    /mnt/shared/xaj/Program/asterinas/tools/docker/run_dev_container.sh \
        --workspace "$KLOG_WORKTREE" --offline -- "$@"
}
klog_dev make test_klog_store TARGET_ARCH=riscv64
klog_dev python3 -m unittest tools/riscv/tests/test_klog_micro_gate.py
klog_dev python3 tools/riscv/diagnostics/klog_micro_gate.py \
    --kernel target/klog-20260908/kernel-final.Image \
    --initramfs target/klog-20260908/micro-v4.cpio \
    --out target/klog-rerun-20260908-01
```

Choose a new `--out` directory for each run; the gate refuses to overwrite evidence.
Use `--smp 1` for the single-core variant.
The gate never builds an image, installs tools, or starts a Firefox session.
It kills its directly launched QEMU process after the default 45-second timeout.

For a kernel-only iteration with the already cached compatible initramfs:

```sh
klog_dev make -o initramfs kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

This deliberately skips rebuilding the large default initramfs;
the ABI probes are compiled and packed separately below.
The kernel image output is `target/osdk/aster-kernel-osdk-bin.Image`.
Freeze a new copy for the next gate instead of overwriting this milestone's evidence.
This kernel-only build is not a claim that the full default initramfs build or CI suite passed.

To rebuild only the small probes, run these commands inside the same container:

```sh
KLOG_CC=/nix/store/qr8b430zfc895sv1a596jvh6pxh62lr2-riscv64-unknown-linux-gnu-gcc-wrapper-14.2.1.20250322/bin/riscv64-unknown-linux-gnu-gcc
KLOG_STAGE=$(mktemp -d target/klog-init.XXXXXX)
cp -a target/klog-20260908/initramfs/. "$KLOG_STAGE/"
make -B -C test/initramfs/src/regression/process/syslog \
    CC="$KLOG_CC" EXTRA_C_FLAGS= HOST_PLATFORM=riscv64-linux
cp test/initramfs/build/initramfs/test/process/syslog/syslog "$KLOG_STAGE/syslog-probe"
"$KLOG_CC" -O2 -Wall -Wextra -Werror tools/riscv/diagnostics/klog_dmesg_probe.c \
    -o "$KLOG_STAGE/dmesg-follow-probe"
"$KLOG_CC" -O2 -Wall -Wextra -Werror tools/riscv/diagnostics/klog_console_probe.c \
    -o "$KLOG_STAGE/console-probe"
cp tools/riscv/diagnostics/klog_micro_init.sh "$KLOG_STAGE/init"
(cd "$KLOG_STAGE" && find . -print0 | cpio --null -o -H newc) > "$KLOG_STAGE.cpio"
```

The cached fixture contains unmodified BusyBox 1.36.1 and util-linux dmesg 2.41.5.
BusyBox and the Nix glibc came from the preceding micro fixture.
The Debian dmesg, loader, libc and libtinfo were extracted read-only with `debugfs`
from `target/firefox-diagnostics-20260908/browser-rootfs-final/debian-root.ext2`.
Neither utility was patched to accommodate this kernel.
The compiler store path is specific to this machine's existing cache;
on another machine select its existing RISC-V compiler and matching runtime explicitly.

## Using it for subsequent development

In a privileged guest shell with util-linux dmesg available:

```sh
dmesg --raw
dmesg --follow --raw
printf '<14>experiment=firefox-ipc phase=start\n' > /dev/kmsg
```

The example marker remains a user-facility record and cannot impersonate a kernel record.
Use independent readers to archive kernel evidence alongside process snapshots.
Do not clear the shared marker during a multi-tool capture unless that is intentional.
For high-volume information/debug capture, select the boot log level explicitly;
raising only the runtime console level does not enable additional capture.

The next useful step is to add a small number of correlated kernel events
around the previously identified Firefox IPC/wait chain
and archive them together with per-thread syscall snapshots.
The new interfaces provide retained evidence and normal tool access;
they do not themselves identify Firefox's remaining root cause.

## Limits and verification not performed

- No physical-board boot, Firefox rerun, journald integration, x86/LoongArch build,
  full regression suite, or sustained-load overhead measurement was performed here.
- Early messages before logger installation are not retained.
  The ring overwrites old records; consumers must account for sequence gaps.
  Timestamps are in microseconds with current millisecond clock resolution.
- Reader notification depends on periodic timer progress.
  This is not a crash-persistent logger or a guarantee of delivery after total scheduler/timer failure.
- `/proc/kmsg`, `/proc/sys/kernel/printk`, configurable ring sizing,
  rate-limit controls, tracefs, perf and eBPF are outside this milestone.
- SCML coverage and compatibility pages were updated.
  The official offline `sctrace` validation attempt could not resolve its uncached `nom` dependency;
  no package was downloaded, and parser validation of those two SCML files is not claimed.
