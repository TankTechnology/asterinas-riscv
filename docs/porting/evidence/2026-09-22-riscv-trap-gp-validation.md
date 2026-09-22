# RISC-V trap-return CPU-local identity validation

## Outcome and claim boundary

The RISC-V kernel trap return no longer loads CPU-owned `gp` from a
potentially stale task stack frame when saved `sstatus.SPP` is set.
User-mode return still restores the user's register.
The main source commit is `50802f1bf`; the isolated build commit is `3aa196979`.

This is an implementation defect and an architectural ownership-contract gap,
not evidence of an incorrect runqueue-selection algorithm.
The deterministic test executes the actual return instructions with a stale
saved value and a valid live base; it does not force a real task migration.
Neither this test nor the finite model proves whole-kernel safety or liveness.
Historical Firefox failures cannot all be assigned to this cause from their
allocator panic sites alone.

## Evidence ledger

All build/test paths below are relative to
`.worktrees/megrez-playback-v2/target/riscv-trap-gp/`, except the model run.

| Check | Observed result | Evidence |
| --- | --- | --- |
| Kernel-return test before fix | Intended assertion failure: observed `gp=0`, expected `18446744071567819792`; 0 passed, 1 failed | `red-kernel/runner.log` |
| User-return test before fix | 1 passed, 0 failed | `baseline-user/runner.log` |
| Kernel-return test after fix | 1 passed, 0 failed | `green-kernel/runner.log` |
| User-return test after fix | 1 passed, 0 failed | `green-user/runner.log` |
| Final fail-closed wrapper | Both exact tests passed, each with nonzero count and runner completion | `ktest.jjoQ3FkD/` |
| Production SV39 SMP=4 build | Succeeded; 12 existing upper-kernel warnings | `build/runner.log` |
| RISC-V OSTD clippy including ktests | Succeeded; existing warnings in `bus/usb.rs` and `io/io_mem/mod.rs` | `clippy-riscv.log` |
| Production ELF | Conditional `gp` restore present; test helper symbol absent | `artifacts/kernel.elf` |
| Bounded model | Correct: 12,961 generated / 2,808 distinct states, queue exhausted; both prescribed negative controls rejected | Main `target/riscv-cpu-local-migration-model/run.an89MCMv/` |
| Independent read-only code review | No actionable findings; ABI, frame layout, CSR restoration and runner checked | Review of `50802f1bf` |
| QEMU Firefox integration | Passed all three device-input/page cycles and final DOM identity; same Firefox service, no restart | `firefox-qemu/` |
| Same-kernel QEMU software reboot | Passed; fresh second firmware epoch and U-Boot prompt, 67.155 s from kernel handoff to recovery | Main `target/qemu-uboot/riscv-trap-gp-recovery/` |
| Plan-bound preboard permit | Passed against the clean isolated commit and exact artifact set | `preboard.json` |
| Megrez integration | Not run with this artifact; authenticated software-reboot path unavailable | Board access below |

The earlier `red/` linker error and `red-linked/` zero-test run are not accepted
as failing-regression evidence.
The helper initially lived in a separate assembly unit, which could not link
the private `_trap_return`; the final helper and return path share one unit.
The old module filter selected zero tests; exact function-name filters fix this.
The first `clippy.log` invocation used the wrong CLI and is not a passing check.

The guest's deliberate red assertion returned host status 0 through the
installed OSDK/QEMU path.
`run-ktest.sh` therefore rejects missing test names, zero tests, failed tests,
caught panics, missing completion, timeouts and nonzero process statuses.
Host exit status alone is not an acceptance signal.

Selected original logs, model counts, desktop results and the third-cycle
screenshot are retained next to this report in `2026-09-22-riscv-trap-gp/`.

## Firefox integration result

The plan-bound stage returned `passed=true`, `reason=desktop-pass` for plan
SHA-256 `4485c16f936fe79ed7f0d07fd4d6e542b1addd28c9c0d011c3dcbc554c0b7070`.
Its native result also returned `passed=true` with three input cycles.
Each cycle correlated four key-down events, absolute pointer movement, a
left-button press/release, DOM state, and 1280x1024 screenshot evidence.
The final third-cycle DOM identity returned status 0.
The retained transcript contains no `Uncaught panic`, `Kernel panic`,
`Printing stack trace`, `RefCell`, `already borrowed` or
`Atomic mode mismatch` marker.

This gate uses emulated devices and a local page, not the physical board and
not Bilibili playback.
The exact command was run in the existing main persistent container so it
could read both the isolated kernel and the existing main rootfs assets:

```sh
docker exec -w /root/asterinas/.worktrees/megrez-playback-v2 asterinas-dev-v1-4f054ba7e4d3-571df973c53d python3 -m tools.riscv.megrez_debug simulate /root/asterinas/.worktrees/megrez-playback-v2/target/riscv-trap-gp/plan-container.json --tier desktop --output-directory /root/asterinas/.worktrees/megrez-playback-v2/target/riscv-trap-gp/firefox-qemu
```

The first `WebDriver:NewSession` took 249.521 seconds by the guest monotonic
timestamps; later sessions took 0.914 and 1.115 seconds.
This identifies remaining cold-start cost, not a measured physical-board
latency or a proven new bottleneck.
Only one fixed-kernel browser integration run was performed in this change.

Retained serial SHA-256:
`d88a34d63da2f78dfe36458940972f64421ebfb6efb89b6ade9a79fe345a523a`.
Third-cycle PNG SHA-256:
`8b77f8838224e1d7274635c14bdf3ca02cfe725af0e802ba06f12685c05c5f40`.

## Software recovery and preboard qualification

The dedicated `generic-sv39-smp4-software-reboot` gate used the same frozen
kernel, the existing TCP-probe initramfs, and the standard bounded probe server.
The initramfs is deliberately different from the desktop Stage1: this gate
checks kernel software recovery, not another desktop cold boot.
Its manifest records initramfs SHA-256
`4eef36c5481e004216f31236e0bc24005aca5dd3c4828f722c40a02042d5939b`.
The TCP probe reached its prescribed completion marker, the 60-second reboot
timer fired, and a second firmware epoch and fresh U-Boot prompt were observed.
The runner exited successfully and cleaned up its QEMU process.

Preparation used the cached U-Boot build through the persistent launcher with
these environment variables and the existing preparation command:

```sh
ASTERINAS_RISCV_BOOTI=/root/asterinas/.worktrees/megrez-playback-v2/target/riscv-trap-gp/artifacts/kernel.Image
ASTERINAS_INITRAMFS=/root/asterinas/target/qemu-uboot/playback-v2-recovery/fs-root/initramfs.cpio.gz
QEMU_UBOOT_PROFILE=generic-sv39-smp4-software-reboot
QEMU_UBOOT_OUT_DIR=/root/asterinas/target/qemu-uboot/riscv-trap-gp-recovery
bash tools/riscv/prepare_qemu_uboot_booti.sh prepare
```

These variables were passed with `env`; export them if reproducing manually.
Choose a fresh output directory for a new run so these originals remain intact.
Run `tools/riscv/qemu_uboot_booti.py run` with that profile, device set
`virtio-net-slirp`, the prepared `boot.ext4`, `artifacts.json`, and
`qemu-dtb-audit.json`, under `ProbeServer(payload_sizes=PROBE_STRESS_SIZES)`.
The run used the cached `target/qemu-uboot/cache/u-boot-build/u-boot` and a
180-second host timeout.

`megrez_debug recovery` bound the result, serial log and preparation
`SHA256SUMS` to `plan-container.json`.
`megrez_debug preboard` then checked the desktop result, recovery result,
root-image contract, DTBs, all input identities, and clean isolated Git state.
The resulting permit is retained alongside this report.
The container initially rejected the bind-mounted worktree's ownership;
a command-local `safe.directory` exception for that exact worktree resolved
the Git check without changing global configuration or relaxing the gate.

The permit is for the existing isolated commit and container-path plan, not
the dirty main tree.
Changing artifacts or translating plan paths requires revalidation/rebinding;
do not hand-edit the permit.

## Reproduction commands

Run from the main repository root:

```sh
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas/.worktrees/megrez-playback-v2 -- bash tools/verification/riscv_cpu_local_migration/run-ktest.sh
bash tools/verification/riscv_cpu_local_migration/run.sh
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas/.worktrees/megrez-playback-v2 -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas/.worktrees/megrez-playback-v2 -- bash -lc 'cd ostd && OSDK_TARGET_ARCH=riscv64 cargo osdk clippy --ktests --features riscv_sv39_mode'
```

The wrapper runs, from `ostd/`, each named test with this command shape:

```sh
SMP=4 CONSOLE=ttyS0 timeout --kill-after=5s 120s cargo osdk test kernel_trap_return_preserves_current_cpu_gp --target-arch riscv64 --scheme riscv --features riscv_sv39_mode
```

The second name is `user_trap_return_restores_user_gp`.
Both tests boot four harts; only the selected test executes.
For the negative control, retain the tests and restore only the old
unconditional `gp` load in a disposable worktree, never in the dirty main tree.

## Frozen artifact provenance

Production files were copied before any later ktest could replace the build
output, and the integration plan binds their hashes.

| Artifact | SHA-256 |
| --- | --- |
| `artifacts/kernel.Image` (15,943,616 bytes) | `ae905fc0f4b78a10175729931ca0d717f2d6a31f5595898b074f74f72d25e8e2` |
| `artifacts/kernel.elf` | `c65a0e435199b53d60c3afee470818326feb50e3335656cebbeb0adb68b28873` |
| Existing browser root image | `bd855c9855e6734cb5e154d488d7c1899f9c1949ac01dae87cd0fc88702e35cf` |

`plan-container.json` records the remaining initramfs, DTB, U-Boot, root
manifest and package identities.
This is the existing playback-v2 baseline plus the focused fix, not a build of
the dirty main workspace.
The isolated baseline has no diagnostic IRQ/preemption resets and retains its
previous affinity-patch reversion.
The user's unrelated main-workspace changes, including a trap-handler far-jump
change, were not bundled into this commit.

The production disassembly at `_trap_return` contains:

```text
ffffffff80ac18c6: andi t2,t0,256
ffffffff80ac18ca: bnez t2,ffffffff80ac18d0
ffffffff80ac18ce: ld   gp,24(sp)
```

Thus a saved kernel SPP bypasses the stale load before temporary registers are
restored.
No allocation, lock, scheduler-policy change or per-CPU state reset was added.
This is a correctness repair; no throughput or latency improvement is claimed.

## Transition audit and prevention rule

The common review question is: can the execution migrate between saving and
restoring this state, and who owns the value at the point of restoration?

| State | Observed implementation | Status / next obligation |
| --- | --- | --- |
| RISC-V kernel `gp` | BSP/AP establish CPU base; task `switch.S` omits `gp`; kernel trap handler can enable IRQs and block | Fixed SPP-conditional return, with actual-instruction regression |
| RISC-V user `gp` | `run_user` saves kernel base; user trap restores it before Rust handling or scheduling | User round-trip regression added; no whole-entry proof claimed |
| RISC-V `tp` | User TLS exposed in `cpu/context.rs`; trap frame saves/restores x4, CPU-local access instead uses `gp` | Not evidence of another CPU-base restore bug; full TLS fidelity audit remains separate |
| RISC-V `sscratch` | Zero in ordinary kernel execution; `run_user` installs user-context frame; trap entry clears it before Rust | Entry/return critical region must stay non-preemptible and non-faulting |
| RISC-V `sstatus` / `sepc` | Saved trap state selects interrupted privilege, interrupt return state and continuation | Tests restore altered CSRs; do not infer verification of every status bit |
| RISC-V `satp` | Activated explicitly through VM-space/page-table path, not loaded from the general trap frame | TLB/ASID/address-space correctness is outside this patch and model |
| LoongArch r21 | `cpu/local.rs` uses r21; `trap.S` reloads its saved value unconditionally | Audit candidate, not a confirmed reachable migration bug: its kernel fault handler does not explicitly re-enable IRQs like RISC-V |
| x86-64 per-CPU GS | Trap/syscall assembly uses privilege-specific `swapgs` paths | Different mechanism; this RISC-V regression is not cross-architecture coverage |

For future trap/context changes, document CPU/task/user ownership, the exact
save/restore sites, the mechanism preventing migration where required, and a
negative regression that violates the contract.
Never clear allocator borrow flags or IRQ/preemption counters merely to make
the downstream symptom disappear.
CPU identity obtained through `gp` cannot independently validate `gp` itself.

## Board access and remaining qualification

The stable FTDI UART responds, so this is not evidence that the board is dead.
It currently exposes `ubuntu-MS-7D76 login:` rather than the documented
`ASTERINAS_DEBUG_CONSOLE_READY uid=0` root shell.
Neither the documented old NixOS default nor one attempt with the RockOS
factory account authenticated; no replacement credential was stored or guessed.
SSH to the known board address refused the connection.
RockOS factory credentials do not unlock the Asterinas browser-root accounts.

No physical reset was requested and no board image was overwritten.
The user was asked whether a logged-in graphical terminal remains available
for software reboot.
Once an authenticated control path is available, use the now-passing
preboard/recovery evidence with this frozen image through the existing
controlled boot workflow, then perform Firefox and muted-video qualification.
QEMU completion does not waive the Megrez hardware gate.
