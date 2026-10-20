# Xorg renderer gate hardening

Date: 2026-10-17

The Xorg modesetting parser previously accepted any `accel=` value. Its test
fixture used `llvmpipe` while still expecting a pass, which could allow a
software renderer to satisfy the Task 5 gate. The parser now requires the
ordered marker `DEBIAN_DESKTOP_DRM_GL_XORG accel=virgl`; missing or any other
renderer is a failure.

## Verification

The focused regression suite passes:

```text
PYTHONPATH=tools/riscv python3 -m unittest -q \
  tools.riscv.tests.test_drm_xorg_modesetting_gate
.....
Ran 5 tests in ...
OK
```

The existing old `target/drm-xorg-modesetting-20261013` artifact is not a new
runtime receipt. It is useful as a negative audit: its serial marker says
`DEBIAN_DESKTOP_DRM_GL_XORG accel=llvmpipe`, and the hardened parser now
returns:

```json
{
  "passed": false,
  "reason": "Xorg renderer is llvmpipe; expected virgl",
  "renderer": "llvmpipe",
  "width": 1280,
  "height": 800,
  "distinct_colors": 1407,
  "non_background_pixels": 986514
}
```

This closes the parser false-positive. The independent QEMU desktop r2 receipt
still supplies the positive `renderer=virgl` runtime evidence; this document
does not promote the old llvmpipe artifact to an accelerated result.
