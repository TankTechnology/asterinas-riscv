# DRM demo on current main: performance implementation plan

**Goal:** Preserve the working Megrez DRM desktop while restoring current-main scheduler behavior and making firmware scanout cost observable without per-frame logging.

**Architecture:** Build an isolated integration branch from main and merge the detached DRM demo head into it. Keep the main scheduler and procfs implementation authoritative. Add aggregate counters only to the firmware framebuffer backend; report at exponentially spaced successful presents through the existing kernel log, so ordinary browser use produces a small, bounded number of records.

**Boundaries:** Do not merge this branch into main, restart the board, alter the live Firefox profile, or claim a speedup from a QEMU run. The current board remains available to the user. The first deployable artifact requires a separate physical handoff with root serial access verified before and after opening the port.

### Task 1: Integrate the DRM demo with main

- [ ] Merge `c0a65e67562132b8f112cb993a96b46962a02583` into `codex/drm-main-perf-20260925` from current main, resolving conflicts in favor of current-main scheduling/procfs semantics and DRM display functionality.
- [ ] Confirm `53c4601a602fe11c426de38909914960c44aa890` and `c0a65e67562132b8f112cb993a96b46962a02583` are both ancestors of the result; confirm `/proc/<pid>/task/<tid>/schedstat` remains in the source tree.
- [ ] Compile the optimized RISC-V Sv39/SMP4 kernel with the persistent project container and run the existing bounded QEMU DRM firmware gate once.

### Task 2: Freeze counter semantics before implementation

- [ ] Add a focused kernel test for a pure scanout accounting type: full and dirty counts are separate; byte counts use checked or saturating arithmetic; max latency never decreases; failed presents do not count as successful.
- [ ] Run the focused test and observe failure because the type is absent.

### Task 3: Add bounded firmware scanout telemetry

- [ ] Time successful `present_framebuffer` and `dirty_framebuffer` calls with the monotonic kernel clock, calculate copied bytes from validated geometry, and update independent atomic aggregate counters after the copy completes.
- [ ] Emit one summary when the total successful present count reaches a power of two. Include full/dirty counts, total bytes, cumulative duration and maximum duration; never log every frame or include browser URLs.
- [ ] Run the focused test, formatting/check, the optimized RISC-V build, and the bounded QEMU firmware gate. Inspect any failures before repeating a gate.

### Task 4: Record the deployment decision

- [ ] Save the integration commit, artifact hash, focused test result, QEMU result, and remaining physical-only uncertainties in a dated evidence note.
- [ ] Leave the existing physical browser running. At the next controlled board boot, read the cumulative counters after normal use and decide whether full-frame copy/cache synchronization or another path merits the next change.
