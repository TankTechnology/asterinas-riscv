# Firefox Marionette NewSession probe: bounded sample timeline

## Current stage

E3 remains a diagnostic stage.  The full Firefox scene gate was not run in
this step.  The probe-only path now records a process/thread/log timeline
around the single `WebDriver:NewSession` request.

## Change

`firefox_drm_scene_gate.py` now:

- captures one process/thread/log baseline immediately before sending
  `NewSession`, so a slow procfs sampler cannot erase the only snapshot;
- captures a terminal process/thread/log sample before Firefox cleanup;
- retains at most four compact timeline samples, with bounded syscall text,
  thread and child counts;
- stores `sample_count` and `samples` in the existing atomically-renamed
  `/run/asterinas-firefox-marionette-probe.json` receipt and reports the count
  in the serial marker.

This is a host-code contract only; no guest was started after the change.

## Verification

```text
python3 -m unittest tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_debian_dev_overlay -q
Ran 49 tests ...
OK
python3 -m py_compile tools/riscv/debian/rootfs/firefox_drm_scene_gate.py \
  tools/riscv/debian/rootfs/desktop_m4_gate.py
git diff --check
```

The latest bounded runtime reference remains r13.  It reached the Marionette
greeting and completed the request write, then timed out waiting for the
response header.  Its final snapshot showed the Firefox parent alive and a
sleeping child in `ppoll(73)`; it did not produce a successful NewSession.
That runtime is retained as diagnosis, not as evidence that the new timeline
code has passed on a guest.

## Artifacts

| Artifact | SHA-256 |
|---|---|
| `tools/riscv/debian/rootfs/firefox_drm_scene_gate.py` | `eae8a02f53bb28a7f9ed1072acd683fea99e1b547386f276458f1c641d8c67f2` |
| `tools/riscv/tests/test_firefox_drm_scene_gate.py` | `12c5ceb98a5f4a32a4388160f5e13e765b018221b34fa0d68dd886398c88a5ad` |
| r13 serial log | `6d82ebbf918b76843b3c43c4bf30f1d984d6d3920fe10341829f4e874c13cd93` |
| r13 result | `c7a75baad77373c0e0202c0bd46a44b345f7fa5929b09c901b0a7f6d07630e4a` |
| r13 lifecycle | `439e99b6e3fab5787b2c3d5c58bdd61c98b4efcdd79613857912df852fcd776f` |

Output group: `/mnt/mydisk/qemu-firefox-futex-args-r13/`

