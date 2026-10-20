# Firefox futex argument contract r11

The short-probe process summary now retains `arg0` through `arg3` for futex
(98) and ppoll (73) syscall snapshots. This is the scalar information needed
to correlate the terminal parent futex with a wake or IPC poll, without
exposing user memory or changing syscall behavior.

Static verification:

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_rootfs.DebianDesktopM4GateTests
Ran 49 tests ... OK
python3 -m py_compile ...
git diff --check
```

This is a host/code contract receipt only. The next bounded runtime probe must
still demonstrate the arguments in a guest `FIREFOX_DRM_PROBE_SYSCALL` or
`FIREFOX_DRM_PROBE_CHILD` marker.
