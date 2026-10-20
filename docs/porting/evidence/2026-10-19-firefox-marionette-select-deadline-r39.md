# Firefox Marionette select-deadline contract r39

This is a host/source-only contract check for the next bounded probe. The
Marionette client no longer relies on the guest `SO_RCVTIMEO` implementation
for response deadlines: `_read_exact` waits for socket readability with
`select.select` and the existing monotonic deadline, then performs `recv`.

The change addresses the observed mismatch where a configured 150-second
probe ended at the response-header boundary after 7--12 seconds, while an
older full gate received NewSession after about 103 seconds. It changes only
the test transport; it does not modify Firefox, the kernel, QEMU, or any
runtime artifact.

## Verification

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 52 tests ... OK
python3 -m py_compile tools/riscv/debian/rootfs/browser_m5_marionette_gate.py \
  tools/riscv/debian/rootfs/firefox_drm_scene_gate.py \
  tools/riscv/tests/test_firefox_drm_scene_gate.py
git diff --check -- tools/riscv/debian/rootfs/browser_m5_marionette_gate.py \
  tools/riscv/tests/test_firefox_drm_scene_gate.py
```

No QEMU or Firefox runtime was started because `/mnt/shared` has 51G free,
below the 60G pre-gate requirement. This receipt therefore proves only the
host transport contract; it cannot satisfy E3.

## Files

- client: `tools/riscv/debian/rootfs/browser_m5_marionette_gate.py`
- test: `tools/riscv/tests/test_firefox_drm_scene_gate.py`
