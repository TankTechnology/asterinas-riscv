# Firefox gate boundary instrumentation

## Scope

After r18 reproduced the same `WebDriver:NewSession` stall, the guest gate was
instrumented to publish a phase elapsed metric and an explicit NewSession
boundary result. This is host/source work only; it does not claim a new Firefox
runtime result and does not start QEMU.

New records are:

```text
FIREFOX_DRM_PHASE_METRIC name=<phase> elapsed_ms=<bounded number>
FIREFOX_DRM_BOUNDARY name=new-session status=pass|error type=<type> elapsed_ms=<bounded number>
```

## Verification

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate -q
Ran 20 tests ...
OK
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py
git diff --check -- tools/riscv/debian/rootfs/firefox_drm_scene_gate.py tools/riscv/tests/test_firefox_drm_scene_gate.py
```

The instrumentation makes the next diagnostic a short boundary probe instead
of a full scene-and-recovery run. Existing r18 remains the authoritative
runtime receipt and E3 stays blocked at NewSession.
