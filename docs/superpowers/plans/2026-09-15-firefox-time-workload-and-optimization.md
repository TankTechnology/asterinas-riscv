# Firefox Time Workload and Measured Optimization Plan

> **For agentic workers:** Implement tasks sequentially in this workspace, with tests before each behavior change. No delegation is required for this plan.

**Goal:** Compare keyboard, pointer, scroll, local navigation, and real-page waterfall latency in one stable Firefox boot, then optimize only a measured kernel or userspace bottleneck.

**Architecture:** Reuse the existing Megrez fixed-payload HTTP server but serve a separate, lightweight performance document so capability checks and manual PASS/reboot gates cannot perturb timing. Browser timing samples distinguish trusted physical input from synthetic browser scheduling. The Stage1 CPU ledger runs concurrently at low frequency. A read-only analyzer binds provenance and refuses to subtract clocks in different domains. Physical evidence selects the first optimization target.

**Tech Stack:** Python 3 standard library, embedded HTML/JavaScript, Firefox Marionette, Asterinas procfs, `unittest`, persistent Docker.

---

### Task 1: Lightweight browser timing document

**Files:**
- Modify: `tools/riscv/megrez_network_fixture.py`
- Modify: `tools/riscv/tests/test_megrez_network_fixture.py`

- [ ] Add failing HTTP tests for an exact `/browser-quality/perf.html` page and its second page, with no query strings, no capability/storage checks, and no legacy request-log entry.
- [ ] Add a bounded JS timing API for keyboard, pointer, and scroll events; tag trusted and synthetic sources separately. Record event-handler-to-first-rAF and event-handler-to-next-rAF, explicitly not physical scanout. Limit each kind to 64 samples and avoid per-event console logs.
- [ ] Add a deterministic local navigation link; the second page reports its Navigation Timing entry without a remote origin.
- [ ] Verify fixture tests and the unchanged browser-quality capability gate.

### Task 2: Fail-closed browser timing contract

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_latency_contract.py`
- Create: `tools/riscv/tests/test_browser_latency_contract.py`

- [ ] Write failing tests for schema, bounded sample counts, finite nonnegative durations, source separation, and navigation ordering.
- [ ] Implement nearest-rank p50/p95 for each of keyboard, pointer, scroll, and local navigation. Reject missing frame or PID/provenance change instead of reporting zero.
- [ ] Verify parser tests and static lint.

### Task 3: One-boot guest capture and waterfall comparison

**Files:**
- Create: `tools/riscv/debian/rootfs/browser_perf_capture.py`
- Create: `tools/riscv/tests/test_browser_perf_capture.py`
- Modify: `tools/riscv/debian/rootfs/build_stage1.sh`

- [ ] Add a separate opt-in non-reboot timing tool after the existing desktop is ready; capture browser samples and selected Firefox/Xorg CPU intervals into a private evidence directory. Keep the Baidu content gate unchanged.
- [ ] Record guest-monotonic command start/end, Navigation Timing (DNS/connect/TLS/first byte/DOM/load), resource-count completion, and provenance for both local and public pages. Public proxy availability is recorded, not silently assumed.
- [ ] Ensure no extra rootfs build or partition-2 write; Stage1 helpers remain under `/run/asterinas-tools`.
- [ ] Run host tests, cached-container QEMU smoke, then one bounded physical boot with multiple samples. Stop on terminal errors while preserving serial logs.

### Task 4: Evidence-driven optimization

**Files:** Chosen only after Task 3 evidence identifies a dominant cost.

- [ ] Classify the dominant delay as input path, browser scheduling, fbdev copy, local socket, proxy/public transfer, or page execution. Keep unsupported measurements explicit.
- [ ] Add a failing regression or microbenchmark for the selected path; inspect Linux/hardware contracts when changing kernel scheduling or framebuffer behavior.
- [ ] Implement one focused fix, rerun the same workload with the same kernel/rootfs provenance (or clearly version both), and report raw plus p50/p95 before/after results.
- [ ] Leave the DRM provider boundary stable for the separate DRM team and do not infer physical HDMI latency from rAF callbacks.
