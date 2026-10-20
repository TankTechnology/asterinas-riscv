# E3 Firefox probe launch-prune contract r125

r123 showed both capability arms timing out before Firefox because the normal
desktop X session also started NetSurf.  The probe bootargs now set
`ASTERINAS_DESKTOP_BROWSER_ENABLED=0`, leaving Xorg/DRM as the only desktop
owner before the Marionette probe.  This changes only the probe workload; it
does not change the ordinary desktop profile.

Static validation passed:

- 38 `test_firefox_drm_scene_gate` tests;
- Python `compileall` for `tools/riscv/debian/rootfs` and `tools/riscv/tests`;
- `git diff --check`.

No QEMU or physical-board run was used for this contract change.  The next
run may use the already materialized r122c rootfs and a fresh 180-second A
arm; only if it reaches the request marker should a B arm be started.
