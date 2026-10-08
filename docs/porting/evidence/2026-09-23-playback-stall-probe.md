# Playback probe state and bounded buffer-starvation experiment

The September 22 playback probe had a reproducible observation bug.
It recreated its Marionette sandbox on every sample,
losing the video wrapper's promise state and event counters.
This explains the recorded combination of advancing video time,
`playPromise=pending`, and zero probe events.
It does **not** establish the cause of the later physical-board loss of UART control.

## Scope and identities

The experiment used QEMU RISC-V virt, four CPUs, 2 GiB RAM,
the existing Debian browser root image, and the original release kernel.
No kernel rebuild, physical-board reboot, or host network change was required.

- Kernel Image SHA256:
  `54d8b57b7a5d97883fb353328c04702ec088adbf2c8bce61d52e29948b79b794`.
- Input root image SHA256:
  `bd855c9855e6734cb5e154d488d7c1899f9c1949ac01dae87cd0fc88702e35cf`.
- Browser-reported version: Firefox 143.0.3, process ID 244 in the final run.
- Media: repository-owned `browser_m5.webm.base64`,
  646 decoded bytes, one second of silent 160 × 90 VP8/WebM.
- Final guest boot ID: `7b8402d9-8954-4dfd-b47c-5e34509eb145`.
- Production `_playback_probe` source SHA256:
  `3f673694b6c4e58ee0c85cd9f8cbacee04fbaa64ce29104c8af1c76111955b8c`.

Full input hashes and QEMU arguments are in the result JSON.
The reported browser user agent is overridden by the existing browser profile;
its x86-64 string is not the guest architecture.

## Short experiments

The first successful run (`qemu-03`) compared the same JavaScript
with fresh/default and persistent/named sandbox settings.
The final run (`qemu-05`) repeated the comparison using the actual patched
Python `_playback_probe` function extracted with `inspect.getsource`.
The function ran with the installed guest gate's normal response validator.
Read-only scripts recreated the default sandbox between playback samples.

| Observation in the final run | Fresh default sandbox | Patched production probe |
| --- | --- | --- |
| Video advances and decodes frames | Yes | Yes |
| Promise completion retained | No; all four samples pending | Yes; resolved |
| Playback event counts retained | No; all zero | Yes |
| Short clip finishes without the probe restarting it | No; later sample returns to time zero | Yes; ended at one second |

Page-owned event listeners independently observed playback in the failing case.
This is a real Firefox comparison, not simulated JavaScript or mocked JSON.
[Mozilla documents sandbox retention and named sandboxes](https://firefox-source-docs.mozilla.org/python/marionette_driver.html#marionette_driver.marionette.Marionette.execute_script).

For buffer starvation, a guest loopback HTTP server supplied the first WebM
segment to a real `MediaSource` and withheld the next response.
The test waited for playback followed by a new `waiting` event.
Only then did the host issue a fresh nonce-framed root command over QEMU serial
to release the second response.
The browser and the guest remained the same throughout.

| Phase | Video time | Buffered end | Ready state | Decoded frames |
| --- | ---: | ---: | ---: | ---: |
| Waiting for withheld response | 0.819131 | 1 | 2 | 4 |
| Response released | 0.819131 | 2 | 2 | 4 |
| Playback resumed | 1.511963 | 2 | 4 | 7 |

While the response was withheld, a Marionette DOM button click completed.
Fresh serial commands returned UID 0 and the same boot ID before,
during, and after the experiment.
Page timer ticks advanced from 14 to 29 across starvation and recovery.
The final experiment phase took 26.334 host seconds,
including Marionette session setup but excluding QEMU/desktop startup.
The guest script exited zero and the host result was `passed=true`.

This models an interrupted supply of media over loopback TCP,
not link loss, a TCP reset, a physical NIC interrupt, or a live Bilibili CDN.
The button check is a DOM command, not a physical keyboard/mouse test.
QEMU's UART also does not exercise the board's DW-APB receive path.

## Change and regression checks

`_playback_probe` now reuses the `asterinas-bilibili-playback` sandbox.
Other default-sandbox DOM probes cannot discard its observation state.
The existing secure-source, decoded-frame, playback-progress,
promise-completion, and event requirements remain unchanged.

Timeout diagnostics now retain the last valid observed media state
and any subsequent probe error.
They no longer claim that the video element was absent
when video time was advancing but other acceptance requirements were missing.
An instantaneous two-sample regression reproduced both the misleading timeout
and loss of the last observation after a communication error before the fix.
The complete `test_debian_browser_web` module passes all 85 tests.

The real-browser regression is intentionally opt-in, outside the fast unit suite.
The archived runner imports the current production function, so reverting its
sandbox settings makes the real promise/event assertions fail.
It reuses the persistent development container and immutable existing images.

## Evidence and replay

The adjacent `2026-09-23-playback-stall-probe/` directory contains
the two successful runs' observations and result JSON,
the unit-test log, and a SHA256-indexed archive of raw serial logs and scripts.
Serial sampling repeats some log lines;
`observations.json` removes byte-identical repeated evidence lines only.
Readable unit logs strip trailing whitespace; the archive preserves raw bytes.
The archive also retains unsuccessful preparation attempts:

- `qemu-01`: manually interrupted during desktop restart after identifying
  shell-framing defects in the temporary runner; no playback outcome.
- `qemu-02`: the temporary 90-second startup limit expired while the existing
  `start-browser` command was stopping the desktop; no playback outcome.
  Subsequent runs reused the live desktop with `start-web`.
- `qemu-04`: temporary guest loader used a Python module name for an installed
  extensionless executable; fixed using `runpy.run_path`; no playback outcome.

These preparation failures are not classified as media or kernel failures.
The desktop-stop delay remains a separate observation to investigate.

To replay in the original workspace/container layout, restore the final
`run.py` and `guest.py` from the archive into
`/root/asterinas/target/playback-stall-20260923/` inside the main persistent container,
create a fresh output subdirectory there, and run from the checked-out worktree:

```sh
PYTHONPATH=. python3 /root/asterinas/target/playback-stall-20260923/run.py qemu-replay
python3 -m unittest tools.riscv.tests.test_debian_browser_web
```

The runner reads the existing `browser-release-plan.json` and uses the listed
artifact paths; replay requires those local artifacts.
Disk images are copied for each run and are not included in the evidence archive.
No QEMU process was left running after the tests.

## Remaining physical incident

Simple buffer starvation did not reproduce the combined playback/UART failure.
The next physical reproduction should use the corrected probe and retain
independent root-console witnesses around the first loss of media progress.
If media stalls but DOM and serial commands complete, investigate media supply
and decoder state; if DOM fails but serial works, capture browser/thread waits;
if serial also fails, preserve CPU/interrupt/lock state through an independently
verified debug path before rebooting.
This experiment provides no evidence that the original physical hang is fixed.
