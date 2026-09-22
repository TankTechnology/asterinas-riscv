# RISC-V Kernel Trap `gp` Implementation Plan

**Goal:** Preserve the executing CPU's kernel `gp` across a sleeping trap handler while retaining exact user-register restoration.

**Architecture:** Select `gp` restoration by saved `sstatus.SPP` in the common assembly return. Test the real return instructions with a stale saved value and separately exercise a U-mode round trip. Keep the existing finite model's assumptions explicit.

**Workspace:** Reuse `/mnt/shared/xaj/Program/asterinas/.worktrees/megrez-playback-v2` and its persistent Docker container. Integrate only the focused fix and tests into the primary workspace after validation.

## Tasks

- [ ] Add a `ktest`-only assembly helper in `ostd/src/arch/riscv/trap/return_test.S`. Construct a 34-word trap frame plus saved `gp`, `sstatus`, and `sepc` in a 16-byte-aligned stack allocation. Enter the actual `_trap_return` with SPP set and SPIE clear. Record the returned `gp`, repair it in assembly, restore CSRs and stack, then return the observation to Rust.
- [ ] Add `kernel_trap_return_preserves_current_cpu_gp` in `trap.rs`. Disable IRQs, use a saved `gp` different from the live base, call the helper, and assert that the observation equals the live base. Also test an unchanged saved base.
- [ ] Add `user_trap_return_restores_user_gp` in `trap.rs`. With supervisor interrupt sources masked, enter U-mode at a supervisor-only instruction address through `run_user`. The resulting instruction fault returns through the real user-trap entry. Check the saved user `gp` and restored kernel base, restoring CSRs before assertions.
- [ ] Run the focused OSTD RISC-V ktests on SMP=4 before the fix. Require an assertion failure in the kernel-return test and a passing user-return test; build errors do not count as red evidence.
- [ ] Add named `SSTATUS_SPP` to the assembly parameters. Replace unconditional `LOAD_SP x3, 3` with `andi t2, t0, {SSTATUS_SPP}; bnez t2, .Ltrap_keep_kernel_gp; LOAD_SP x3, 3; .Ltrap_keep_kernel_gp:` before restoring temporary registers. Document CPU ownership and sleeping page-fault handlers.
- [ ] Re-run the focused tests and TLA+ runner. Require green tests and the two expected model counterexamples.
- [ ] Build the SV39 SMP=4 production kernel, verify test helper symbols are absent, and run the existing bounded Firefox QEMU gate with an immutable artifact manifest.
- [ ] Update the code correspondence and record exact commands, artifact hashes, results, and remaining board qualification status. Integrate the focused source diff without disturbing existing dirty files.
- [ ] Follow the existing controlled boot/preboard checks for Megrez validation. A board or integration failure is recorded with its actual cause; do not label the whole Firefox problem solved from the assembly test alone.

## Commands

Build and run OSTD tests using the persistent launcher from the repository root:

```sh
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas/.worktrees/megrez-playback-v2 -- env SMP=4 CONSOLE=ttyS0 cargo osdk test -p ostd arch::riscv::trap::trap::tests --target-arch riscv64 --scheme riscv --features riscv_sv39_mode
bash tools/verification/riscv_cpu_local_migration/run.sh
tools/docker/run_dev_container.sh --workspace /mnt/shared/xaj/Program/asterinas/.worktrees/megrez-playback-v2 -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode
```

Inspect the tool's CLI and existing gate manifest before adapting command-line arguments. Store new evidence under a fresh `target/riscv-trap-gp/` directory; preserve previous playback evidence.
