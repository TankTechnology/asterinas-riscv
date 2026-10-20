# Firefox short-probe socket evidence r94

- Gate: `firefox-marionette-probe-socket-parser` (diagnostic supplemental; does not close E3)
- Scope: host-only parser and receipt instrumentation; no QEMU, Firefox, serial, or hardware run
- Hypothesis: r93 reached `FIREFOX_DRM_GLXTEST ... RENDERER virgl` but the Marionette connection was refused. A bounded `/proc/net/tcp*` snapshot distinguishes a listener absence from a browser process that is alive but not listening.

## Change

`tools/riscv/debian/rootfs/firefox_drm_scene_gate.py` now parses only the requested
Marionette port (`2828`) from bounded `/proc/net/tcp` and `/proc/net/tcp6` reads.
The short probe records the result in its process receipt and emits
`FIREFOX_DRM_PROBE_SOCKET port=2828 states=...` at each sample. Malformed rows
and unavailable proc files are represented as empty/error records and do not
abort receipt publication.

## Verification

The following checks passed in the persistent project container
`asterinas-qemu-gl-env-r2`:

```text
PYTHONPATH=tools/riscv python3 -m unittest -q \
  tools.riscv.tests.test_firefox_drm_scene_gate \
  tools.riscv.tests.test_refactor_reference_scan
Ran 34 tests in 0.020s
OK

python3 -m compileall -q tools/riscv

bash tools/riscv/firefox_fast_check.sh
Ran 333 tests in 26.506s
OK
FIREFOX_FAST_CHECK_PASS
```

The host-side copy of the fast check was not used as a pass result: it lacked
`/usr/local/qemu/bin/qemu-img` and failed one pre-existing qcow2 test. The
container run above is the authoritative result.

## Interpretation and next gate

This closes only the diagnostic parser/unit-test assertion. It does not claim
an E3 runtime pass and does not replace r93's failure receipt. The next bounded
runtime step is one short `PROBE_ONLY=1` run using the existing r93 overlay,
with the new socket marker inspected alongside the already captured process,
thread, syscall, and Firefox logs. A `tcp/tcp6` `0A` entry would prove a live
listener at the sampling boundary; `none` would prove that the refusal occurs
before any listener is established.
