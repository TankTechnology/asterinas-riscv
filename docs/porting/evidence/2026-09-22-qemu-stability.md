# QEMU stability investigation, 2026-09-22

The first bounded QEMU investigation did not reproduce the physical playback
stall. Both hosts completed the scheduling, signal, pipe, migration/page-fault,
and serial-response workloads. The developer host also completed three Firefox
keyboard/pointer interaction cycles. This is a passing baseline, not a fix or a
long-duration stability certification.

## Baseline and scope

- Clean source: `.worktrees/megrez-playback-v2`, commit
  `3aa196979a4330b78a2453ddb567e24b4d85a5a1`. Its Git status remained clean.
- Frozen release Image:
  `target/megrez-debug/firefox-physical-20260922-m80mt2pz/release-artifacts/kernel.Image`;
  SHA-256 `54d8b57b7a5d97883fb353328c04702ec088adbf2c8bce61d52e29948b79b794`.
  The standalone userspace experiments and Firefox gate used this same Image.
- OSTD and UART ktests were separately built from that clean source, release,
  RISC-V SV39, SMP=4, using the existing persistent development container.
- Standalone workloads: QEMU virt, TCG, four vCPUs, 2 GiB, no disks/network;
  generic SV39 CPU (`rv64,sv48=false,svpbmt=true,zkr=true,svadu=false,svade=true`).
  Each run had a 240-second guest process deadline and a fresh protocol nonce.
- RockOS remained the board's host OS. Remote experiments ran as `debian`,
  nice 10, one QEMU at a time. No physical boot changes or board reboot occurred.
- Firefox used scratch QEMU boot/root image files, emulated graphics/input and
  user-mode networking. Its test page was local; network fixture request count
  was zero. This run does not validate Bilibili or external networking.

## Observed results

Times are host wall-clock seconds for these individual runs, not a controlled
performance comparison between the two architectures.

| Workload | Scope per host | Developer QEMU | RockOS QEMU |
|---|---|---|---|
| Boot/syscall probe | Nonce response; syscall 213 expected ENOSYS | — | Pass, 3.238 s |
| Condition-variable handoff | 32 children, 131,072 handoffs | Pass, 4.672 s | Pass, 51.544 s |
| CPU affinity | 8 invocations | Pass, 0.716 s | Pass, 3.120 s |
| Affinity inheritance | 4 invocations | Pass, 0.766 s | Pass, 3.070 s |
| Pipe availability | 32 children | Pass, 0.867 s | Pass, 3.821 s |
| Group stop/futex | 11 cases in one invocation | Pass, 7.226 s | Pass, 9.580 s |
| Serial during handoffs | 64 children, 262,144 handoffs | Pass, 8.833 s | Pass, 106.941 s |
| Migration + usercopy faults, final test | 4 workers × 512 rounds | Pass, 1.017 s | Pass, 6.624 s |
| Harness negative control | Missing executable, child exit 127 | Correctly rejected | Correctly rejected |

The serial experiment received 33 nonce-framed responses during developer-host
load and 416 during RockOS-hosted load. Both received a separate response after
the workload completed and then exited successfully. This exercises continued
guest command processing, not just serial output.

Additional developer-host validation:

- OSTD: **308 passed, 0 failed, 0 filtered**, followed by `All crates tested`.
  The initial runner inventory includes other crates that are skipped; 308 is
  the actual OSTD executed count.
- UART component: **80 passed, 0 failed, 0 filtered**, runner completed.
  The first online build stalled fetching dependencies and was cancelled; only
  the subsequent successful offline run is counted.
- Firefox desktop gate: `passed=true`, `physical=false`, three verified cycles,
  each with four key-down events and one pointer down/up pair, nonce and
  screenshot evidence. Guest root debug console reported UID 0, systemd PID1,
  active graphical/desktop targets and `/dev/vdb` ext2 root. This was QEMU access,
  not a physical-board console handoff or reboot-persistence test.

## Permanent regression change

Added `test/initramfs/src/regression/process/cpu_affinity/migrate_fault.c` and
registered it in both process and CPU-affinity runners. Four workers repeatedly
pin themselves to successive allowed CPUs, read pipe data into two untouched
anonymous pages across their boundary, and verify payload, CPU placement and a
volatile thread-local cookie. Each invocation has a 90-second alarm. Fewer than
two allowed CPUs returns skip status 77, which both runners handle.

This combines migration and kernel usercopy faults; it does **not** guarantee
that migration happens inside a page-fault handler.

The final source compiled with `-O2 -pthread -Wall -Wextra -Werror`, passed on
native Linux and both Asterinas QEMU hosts, and passed clang-format validation.
A disposable mutation corrupting a received byte failed on all four workers
with exit 1. A native single-CPU run returned 77. Shell syntax and patch
whitespace checks passed. An independent review identified the missing standard
runner registration, which was added before the final verification. The complete
process regression suite and full repository lint matrix were not run.

## Findings and limits

`Switching to a task already running in foreground` occurred in successful
handoff and console runs on both hosts, including 96 warnings in RockOS's initial
handoff run and 93 in its console run. Therefore that warning alone does not
establish the earlier suspected deadlock.

The baseline scheduler still has a documented gap between selecting the next
task under the runqueue lock and switching. The processor waits for the next
task's ownership flag and releases the previous task's flag after switching.
Migration keeps an IRQ guard across its scheduling changes; preemption IPIs are
asynchronous. These remain audit points, not a reproduced causal explanation.
The related historical design discussion is
[upstream issue #1633](https://github.com/asterinas/asterinas/issues/1633).

QEMU's default NS16550A receive path differs from Megrez's DW-APB path, which masks
receive interrupts and defers processing through Taskless callbacks. UART ktests
cover simulated register behavior, not the full physical interrupt/controller
chain. Neither those tests nor this roughly 107-second longest standalone run
establish physical playback stability. No speculative kernel fix was applied.

The next useful physical investigation is to sample scheduler progress alongside
DW-APB receive/deferred-callback progress during playback, retaining the verified
root development console and recovery path. If a QEMU workload stalls first,
preserve all-vCPU state and serial progress before changing kernel behavior.

## Reproduction and evidence

The adjacent `2026-09-22-qemu-stability.tar.gz` stores result/request JSON, serial
logs, native controls, ktest logs, Firefox screenshots/plan, exact initramfs
snapshots and disposable runner/build sources. `SHA256SUMS` inside it inventories
the archived files. Large browser root images and kernel binaries are excluded;
their input hashes are recorded in the results.

Working evidence lives under `target/qemu-stability-20260922/`. Initramfs versions
are kept distinct:

| Evidence label | Snapshot | Initramfs SHA-256 |
|---|---|---|
| `initial` | `fixture-v1` | `f37bbbe50f59818cdf6acfb2f37dfbc47631b3f37a4e155f52dcdbf40ada2ba7` |
| `console` | `fixture-console` | `c6360cc7e0e0cd25a526ce419fb4f1384001e2c7b02e41c3eadc5a0c249af935` |
| `migrate-fault` (before volatile TLS refinement) | `fixture-migrate-initial` | `bb64ede4008cd4c081b1ad8c12dfff401fbf7c7bddbaacb70c096655e4a45527` |
| `migrate-fault-final` | `fixture` | `bc1767f9149c4a5c526f274ca2b299b60a5ffeee6e51cdd2fe417078d64455bb` |

The disposable scripts retain this checkout's paths and persistent container
names. `run.py` always uses `fixture/initramfs.cpio`; select the matching archived
snapshot in an isolated reproduction directory to reproduce an earlier version.
Rebuilding a cpio can change metadata and its hash. Run labels must be fresh:

```sh
docker exec asterinas-dev-v1-2506e516cbb6-571df973c53d \
  bash /root/asterinas/target/qemu-stability-20260922/build.sh
PYTHONPATH=. python3 target/qemu-stability-20260922/run.py \
  developer migrate-fault --label repeat-unique
PYTHONPATH=. python3 target/qemu-stability-20260922/run.py \
  rockos migrate-fault --label repeat-unique
```

The rootfs container is used for C compilation because it has cross-libc headers;
the ordinary development image lacked them. All source/build work stays in the
existing containers. Kernel test commands inside the clean worktree container:

```sh
cd /root/asterinas/ostd
SMP=4 CONSOLE=ttyS0 timeout -k 5 300 cargo osdk test \
  --release --target-arch riscv64 --scheme riscv --features riscv_sv39_mode
cd /root/asterinas/kernel/comps/uart
CARGO_NET_OFFLINE=true SMP=4 CONSOLE=ttyS0 timeout -k 5 180 cargo osdk test \
  --release --target-arch riscv64 --scheme riscv --features ostd/riscv_sv39_mode
```

Firefox command, inside the main-workspace container with the clean worktree as
working directory:

```sh
python3 -m tools.riscv.megrez_debug simulate \
  target/qemu-stability-20260922/browser-release-plan.json \
  --tier desktop --output-directory target/qemu-stability-20260922/browser-repeat-unique
```

Final fresh SSH check: RockOS kernel `6.6.87`, boot ID
`7fc45632-f952-4dd1-b3b7-6c7646af958e` unchanged, 9,402 MiB available memory,
and no remaining `qemu-system-riscv64` process. SSH command access is working.
Physical UART root access/reopen/reboot persistence was not retested in this run.
