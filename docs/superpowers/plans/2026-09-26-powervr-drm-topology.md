# Independent DRM devices for Megrez Implementation Plan

This is the first, bounded implementation step of the GPU roadmap. It does
not by itself provide a PowerVR driver or GPU-accelerated Firefox.

**Goal:** Make sysfs represent the EIC7700 display and PowerVR renderer as independent DRM devices without exposing an unimplemented PowerVR node.

**Architecture:** Keep the existing `card0`/`renderD128` runtime behavior during this change. Give the sysfs builder an explicit per-device descriptor containing that device's nodes, driver, and modalias. Its current caller supplies one descriptor; a later PowerVR driver can add a second one after its ioctl, memory, firmware, and synchronization contracts work.

**Tech Stack:** Safe Rust kernel device/sysfs code, Asterinas kernel tests, cached project Docker build.

---

### Task 1: Specify independent per-device sysfs topology

**Files:** Modify `kernel/src/device/sysfs.rs` tests.

- [x] Add a `#[ktest]` that builds two descriptors: `card0` only with driver `simpledrm`, and `card1` plus `renderD128` with driver `pvrsrvkm`. Assert that `/sys/dev/char/226:128/device/drm` contains `card1` and excludes `card0`, while `/sys/dev/char/226:0/device/drm` contains `card0` and excludes `card1`. Assert each `device/uevent` has its own `DRIVER=` and `MODALIAS=`.
- [x] Run the test against the old flat-node builder. The old builder listed all three nodes under each device; the assertion entered the failing kernel-test path, which hung without a normal result line, so the QEMU process was stopped. The green run below is the conclusive executable test.

### Task 2: Make the builder device-aware

**Files:** Modify `kernel/src/device/sysfs.rs`.

- [x] Introduce `DrmSysDevice<'a> { nodes: &'a [(&'static str, u32)], driver: &'static str, modalias: &'static str }`.
- [x] Change `build_dev_node` to iterate over descriptors, then each descriptor's nodes. Build `device/drm` from that descriptor's nodes alone. Format the device uevent from its own driver and modalias; keep node uevents keyed to their own major/minor/name.
- [x] Pass one descriptor for today's `dri::exposed_nodes()` in `init_in_first_process`. Update existing tests to use that descriptor; do not register a PowerVR device in this task.
- [x] Run the targeted kernel test, then `tools/docker/run_dev_container.sh --workspace /home/ubuntu/.codex/asterinas-main-publish -- make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode` and `make test_riscv_drm_render_node_unit`. The target QEMU test passed (1/1), the kernel build passed, and all 14 host unit tests passed. The broad `make ktest` wrapper continued into unrelated crates and hit its 180-second bound after the target passed; this is not a full-suite pass.
- [ ] Commit the isolated topology change to `main` and push it.

### Task 3: Preserve and document the runtime boundary

**Files:** Modify `docs/performance/2026-09-26-megrez-gpu-readiness-check.md`.

- [x] Record that current Asterinas still exposes one software-display DRM device. The two-device sysfs test proves the topology can represent PowerVR later; it is not evidence that the GPU driver, Firefox compositor, or HDMI is accelerated.
- [x] Run `git diff --check`.
- [ ] Verify `git status` is clean after the documentation commit.

The wider GPU work continues in [the hardware roadmap](../../performance/2026-09-25-megrez-display-acceleration-roadmap.md): implement the vendor bridge's observed memory/firmware/submission/sync contract, bind a real PowerVR render device, validate hardware pixels and resource lifetime, then enable Xorg and Firefox GPU paths in selected boots.
