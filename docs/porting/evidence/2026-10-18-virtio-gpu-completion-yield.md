# Virtio-gpu completion wait fairness (2026-10-18T14:37:44Z)

## Root cause boundary

The Firefox r10 serial receipt shows `RenderThread::InitDeviceTask is slow` and
`NewRenderer::Run is slow`, while the guest DRM EGL/GLX probes can independently
reach virgl/renderD128. The common virtio-gpu control queue was bounded only by
one million `spin_loop()` iterations. DRM requests from a scheduled userspace
Firefox thread could therefore monopolize a CPU while waiting for a control
queue completion.

## Bounded change

`kernel/comps/virtio/src/device/gpu/device.rs` keeps the existing finite
one-million-iteration fail-closed bound, but every 4096 iterations calls
`ostd::task::Task::yield_now()` when a scheduler task exists. Bootstrap code
without a current task remains a pure spin, preserving early boot behavior.
Both control and cursor completion loops use the same helper, and a ktest
covers the interval boundary (zero, just-before, and repeated interval).

## Verification

- `cargo check --target riscv64imac-unknown-none-elf -p aster-virtio` in the
  persistent development container: passed.
- The same target check with `RUSTFLAGS='--cfg ktest'`: passed, compiling the
  new ktest module.
- `git diff --check` for the virtio-gpu source: passed.
- No QEMU, Firefox, serial, or physical-board run was started for this kernel
  change. A full riscv64 kernel image and runtime Firefox gate are still needed
  before claiming E3 progress.

## Superseded by r11

The subsequent one-time runtime test showed that this implementation called
`Task::yield_now()` while holding the virtio control-queue spin guard, causing
`MIGHT_SLEEP_VIOLATION` and a kernel panic. The helper, ktest, and new kernel
artifact were reverted; the original bounded spin path is the retained source
state. See `2026-10-18-qemu-firefox-policy-r11-kernel-yield-panic.md` for the
runtime evidence.

## Lock-safe replacement (r12)

The retained implementation now uses the existing wait-queue `Mutex` for the
control and cursor queues. The shared DMA buffer remains protected while the
queue guard is held, and completion polling can yield without violating the
atomic-section rule. Riscv64 checks with and without `ktest` pass. Runtime r12
contains no panic, but EGL and Firefox `NewSession` still time out; see
`2026-10-18-qemu-firefox-policy-r12-mutex-queue-timeout.md`.
