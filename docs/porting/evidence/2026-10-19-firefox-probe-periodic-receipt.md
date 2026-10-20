# Firefox probe periodic receipt instrumentation

The short probe now emits bounded child-process and Firefox/moz-log byte/hash
records every sampling interval while `NewSession` is pending.  This keeps
the diagnostic evidence available when a host timeout terminates QEMU before
the guest `finally` block can publish `/run/asterinas-firefox-marionette-probe.json`.

New serial markers:

```text
FIREFOX_DRM_PROBE_SAMPLE_CHILD pid=... name=... state=... threads=... wchan=... syscall=...
FIREFOX_DRM_PROBE_SAMPLE_LOG browser_bytes=... browser_sha256=... moz_bytes=... moz_sha256=...
```

The child scan remains capped at eight records per interval; the underlying
proc scan is still capped at 256 entries and 64 retained children.  No Firefox
behavior or acceptance rule changed.

## Verification

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 46 tests ...
OK
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py
git diff --check -- <changed files>
```

| File | SHA-256 |
|---|---|
| `tools/riscv/debian/rootfs/firefox_drm_scene_gate.py` | `1b99295fd9b8aea68ad5e850129f11ea6667166923d9a0095b4dcc41a408d355` |
| `tools/riscv/tests/test_firefox_drm_scene_gate.py` | `406b4668873524544e8a2b88f4ba146a2815be3e6295b1b6ed968a1b3416c22b` |

This is host/source evidence only; r15 remains the latest runtime probe.
