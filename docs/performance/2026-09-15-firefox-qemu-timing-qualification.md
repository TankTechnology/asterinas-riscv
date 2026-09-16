# Firefox QEMU timing qualification, 2026-09-15

This is a QEMU experiment record, not a physical Firefox performance result.
It used the already cached Debian `browser-web` root image, the persistent
Docker development container, and the Asterinas kernel image with SHA-256
`0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b`.
The final rebuilt Stage1 archive has SHA-256
`764a18a2665dfe76a45b983bc86ac58751ecb257141326668787f4a036ed0d13`.
There was no physical-board or partition operation in this experiment.

## What passed

The fresh QEMU `browser-web` debug-console gate passed with Debian 13.7,
systemd PID 1, active graphical/desktop probes, and 20 requests of 65,536
bytes each to the local fixture.
The separate guest `/proc/stat` and `/proc/<pid>/stat` sampler ran with a live
Firefox PID 164 and Xorg PID 112.
Across four 0.5-second windows, Firefox consumed about 570, 590, 530, and
490 ms of aggregate CPU time; CPU 2 was busy for about 100%, 100%, 100%, and
98% of those windows.
These are startup-time QEMU observations only.
They do not isolate a kernel scheduler cost or measure physical input latency.

## Strict timing failure

The local Firefox Marionette capture reached its second page, but the
Navigation Timing quality gate rejected the browser snapshot:

```text
startTime=0 ms
fetchStart=-11 ms
responseStart=253 ms
responseEnd=254 ms
domContentLoadedEventEnd=948 ms
loadEventEnd=950 ms
validation="fetchStart is out of bounds"
```

The diagnostic marker and terminal reason are in the private QEMU serial
artifact at
`target/physical-firefox-validation/qemu-perf-number-diagnosis-20260915/debug-root-console.serial.log`.
The browser-web gate result and CPU ledger are under the adjacent
`qemu-system-time-20260915` and `qemu-time-ledger-20260915` directories.
The final Stage1 archive was rebuilt after tool formatting; no further QEMU
run is claimed for that byte-identical-behavior rebuild.

The [Navigation Timing Level 2 specification](https://www.w3.org/TR/navigation-timing-2/)
defines these timestamps relative to the navigation time origin.
Mozilla has separately tracked negative Firefox `fetchStart` values in
[bug 1429422](https://bugzilla.mozilla.org/show_bug.cgi?id=1429422) and
[bug 1843850](https://bugzilla.mozilla.org/show_bug.cgi?id=1843850).
The observed `-11 ms` is consistent with that class of browser anomaly, but
this experiment does not independently rule out an Asterinas timekeeping
contribution.

Per the user's decision, the capture contract is unchanged:
negative `fetchStart` remains a failure, is not clamped to zero, and does not
become a partially accepted performance result.
No kernel performance fix is justified by this evidence alone.
The next qualifying experiment should compare Firefox's timing on the same
local workload against a Linux control, while separately collecting physical
keyboard, pointer, and page-load measurements when the board is available.
