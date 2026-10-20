# Firefox syscall argument coverage r47

The guest already persists all six scalar syscall arguments in
`/proc/<pid>/task/<tid>/asterinas_syscall`. The Firefox probe summary was
discarding arguments for the I/O and process-startup calls needed to diagnose
the remaining E3 boundary. The summary now exposes arguments for mmap,
read/write, openat, ioctl, clone/exec, ppoll, epoll, futex, and related file
metadata calls while retaining the existing bounded four-argument output.

Verification:

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 53 tests ... OK
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py
git diff --check -- tools/riscv/debian/rootfs/firefox_drm_scene_gate.py \
  tools/riscv/tests/test_firefox_drm_scene_gate.py
```

This is a host/source contract only. It does not claim a new Firefox runtime
result; the next materialized probe can correlate Web Content mmap/open/read
and scheduling waits directly.
