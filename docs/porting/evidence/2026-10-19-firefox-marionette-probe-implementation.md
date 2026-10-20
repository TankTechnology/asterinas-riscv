# Firefox Marionette short-probe implementation

## Current stage

E3 diagnosis is split from the full scene gate. The new probe-only service
starts the official Firefox, waits for the Marionette greeting, sends exactly
one minimal `WebDriver:NewSession`, and stops at a 90-second probe deadline.
It does not navigate, capture a screenshot, collect frame samples, or run
recovery.

At the failure boundary it atomically writes
`/run/asterinas-firefox-marionette-probe.json` and emits bounded serial
records for:

- Firefox process status, RSS, `wchan`, and thread count;
- up to 128 thread `status`, `wchan`, and `stat` records;
- browser and MOZ log byte counts, SHA-256 values, and capped tails.

## Artifacts

- Guest probe implementation: `tools/riscv/debian/rootfs/firefox_drm_scene_gate.py`
- Probe service: `tools/riscv/debian/rootfs/firefox_marionette_probe.service`
- Probe overlay: `tools/riscv/debian/rootfs/firefox_marionette_probe_overlay.json`
- Materialized rootfs: `/mnt/mydisk/asterinas-firefox-marionette-probe-rootfs-20261019`
- Rootfs image SHA-256: `3cc0ba5080fa05bb998b110cefa31406c1e5b9931be0298545a996dc1e952776`

## Verification

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 44 tests ...
OK
python3 -m py_compile firefox_drm_scene_gate.py desktop_m4_gate.py
```

The probe QEMU run was intentionally not started after the pause instruction;
no runtime result is claimed here. E1/E2/E4 remain green; E3 remains blocked
at the previously recorded NewSession boundary.
