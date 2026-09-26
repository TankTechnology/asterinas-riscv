# Megrez PowerVR device-tree probe implementation plan

**Goal:** Add a safe, opt-in Asterinas diagnostic for the prepared Megrez GPU
resource contract.

**Architecture:** A small RISC-V DRM sibling module inspects the boot DTB and
emits one status line. A pure shape validator allows tests without mapping GPU
registers. The existing DRM runtime remains unchanged.

**Tech Stack:** Safe Rust kernel, `fdt` node properties, RISC-V QEMU kernel
tests, cached Docker build.

---

### Task 1: Test the resource contract

**Files:** Create `kernel/src/device/dri/powervr_probe.rs`; modify
`kernel/src/device/dri.rs`.

- [x] Write kernel tests for the accepted tuple `(0x51400000, 0xfffff, 24,
  60, 4, true, true)` and rejection of each changed field. Test the QEMU DTB
  path returns `no_gpu_node` without reading MMIO.
- [x] Run the filtered QEMU kernel test and observe a failure before adding the
  validator.

### Task 2: Implement the opt-in diagnostic

- [x] Add `powervr_probe.rs` under `#[cfg(target_arch = "riscv64")]` and call
  its entry point once from `dri::init_in_first_kthread`, before display
  selection. The first operation checks `asterinas.gpu_dt_probe=1`.
- [x] Inspect `DEVICE_TREE` for `img,gpu`; validate the first `reg`, property
  byte lengths, `dma-noncoherent`, and `status`. Emit one bounded serial status line
  that remains observable with `loglevel=off`.
  Do not call `IoMem::acquire`, create `/dev/dri` nodes, or change clocks.
- [x] Run the focused kernel tests, RISC-V kernel build, and existing DRM host
  tests. Save QEMU output and `git diff --check` result.
- [ ] Document the non-hardware boundary and commit/push on `main`.

### Task 3: Physical gate

- [ ] Prepare a selected boot with the exact known-good DTB and a single
  `asterinas.gpu_dt_probe=1` argument. Confirm the staged Image/DTB hashes
  before boot and retain default RockOS recovery.
- [ ] Capture the probe line plus fresh UID 0/boot ID responses; close/reopen
  serial and verify again. Reboot back to default RockOS if the selected boot
  becomes unstable. Keep the board in a controllable state.

This plan ends at DT validation. A later plan must prove and own the GPU
clock/reset/firmware sequence before any identity-register read.
