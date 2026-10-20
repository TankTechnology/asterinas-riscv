# Firefox short-probe child-process instrumentation

This is a host-side implementation record; it does not claim a new QEMU or hardware run.

## Change

- The `PROBE_ONLY` receipt now records bounded direct Firefox children in addition to the browser process.
- Each child record includes PID, name, state, thread count, RSS, wait channel and scheduler counters.
- Serial output emits `FIREFOX_DRM_PROBE_CHILD`; scan is capped at 256 `/proc` entries and 64 children.
- Child records include a bounded `asterinas_syscall` snapshot and its non-sensitive summary; r12 additionally enabled futex/syscall profiling only for the probe boot.
- Existing atomic receipt publication and browser/moz log snapshots are unchanged.

## Verification

```text
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate tools.riscv.tests.test_debian_dev_overlay -q
Ran 44 tests in 5.411s
OK
git diff --check -- tools/riscv/debian/rootfs/firefox_drm_scene_gate.py tools/riscv/tests/test_firefox_drm_scene_gate.py
```

## Runtime boundary

The last runtime receipt remains r6: Marionette greeting passed, `NewSession` timed out, and no new runtime was started for this change.

| file | sha256 |
|---|---|
| `tools/riscv/debian/rootfs/firefox_drm_scene_gate.py` | `6308c9744b3a18662fc3c485f985bc02105407817bc069c9c3edad7c516f9ece` |
| `tools/riscv/tests/test_firefox_drm_scene_gate.py` | `f574bad65a197d3a03692bdf00dbd5c1209cf3fe0895504620075a9606e8d94d` |
