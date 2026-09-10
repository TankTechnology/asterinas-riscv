# Physical graphics setup diagnostics (2026-09-08)

## Status and scope

The current-source three-cycle QEMU interaction gate has not passed.
The original current-kernel runs fail before the first READY marker.
The historical-kernel readiness control reaches READY, then fails while
reading the page snapshot; it does not complete an interaction cycle.
No result in this record establishes real keyboard, mouse, or HDMI acceptance.
The physical board gate has not been launched from these diagnostic results.

The working branch is `codex/megrez-physical-graphics-current-main`,
with HEAD `cc66b9d0b2b90c046abd7ffa6db2fb0e86a9e2d0`
and uncommitted setup-budget, setup-order, and local-profile isolation changes.
Evidence paths below are relative to this branch's worktree,
under `target/current-main-physical-graphics/`.
Each run retains its actual input hashes and QEMU arguments in `result.json`.
Runtime-only diagnostic overrides are not production fixes.

## Frozen baseline

| Input | SHA-256 |
| --- | --- |
| Kernel, Sv39 SMP=4 | `5689ba879ee3b50a9f9c68d22f7af7df9b30e0674e750a8fc1c4bf1c41127f74` |
| Browser-web rootfs | `0fc120945b910923225d0fceafdd59945a143b45d5608a19b798225be2b7a191` |
| Rootfs manifest | `6363880f00cd5317bf97f4b3bc749271edcd366f0a5ae2596e202f669b598d9b` |
| Package lock | `d4381641088a10429b26ac478e43e2d7b54ca26b9be0a144a5e6e1e418449c23` |
| Package checksums | `734835671bcca416329fd4f86bf471be8261cd798903131a0ca9c9e9d5c99f46` |
| Stage1 initramfs | `37465c5579547d5f00a0db8fbe5d30cea85a553ecaaff08d611d819ce934555f` |
| QEMU DTB | `fbf606c939b855f06a11d66f3206be46552ba7ba0fe58240231d3b50a891aef8` |
| U-Boot | `ff9abcc4d04ecae76645ddd8f42a61d314938a66f98e91878faedb2633d76b62` |

The package lock contains `firefox-esr 140.15.0esr-1~deb13u1`.
The guest page SHA-256 is
`a9cbc0677a70dcd41a02af4daf66d9008e8bf24ecd1e376aa01d96646ff731bd`.
The HTTP diagnostic checks that its response body is byte-identical
to this page inside the frozen ext2 image before starting QEMU.

The current persistent container is
`asterinas-dev-v1-4f054ba7e4d3-a96860817385`.
The previous image and this container contain the same QEMU 10.2.1 binary,
SHA-256 `b7c12a6612f1101d1c7acc4f39fcac92bd4ccf513d91c6f60647b8bf7d4aaa12`.
These diagnostic runs reuse installed tools and existing artifacts;
they do not reinstall OSDK, rebuild the kernel/rootfs, or download packages.

## Completed negative experiments

| Evidence directory | Change | Observed result |
| --- | --- | --- |
| `qemu-physical-graphics-background-suppressed` | Local graphics profile; competing external services masked | NewSession, window handles, and navigation return; first focus ExecuteScript times out |
| `qemu-physical-graphics-rcu` | Isolated RCU experiment, kernel `e93417d9…` | Failure before READY; this run used the older rootfs `1a088ecc…` |
| `qemu-physical-graphics-time-experiment` | Historical coarse-clock variant, kernel `378718ba…` | Same first-script timeout with baseline rootfs |
| `qemu-physical-graphics-periodic-timer-experiment` | Revert high-resolution RISC-V timer deadlines, kernel `d5778099…` | Same first-script timeout with baseline rootfs |
| `qemu-physical-graphics-http-origin` | Change only page URL from file to host-local HTTP | HTTP 200, then same first-script timeout with baseline kernel/rootfs |

The RCU run is not a same-rootfs A/B against the latest baseline.
None of these negative experiments proves the tested subsystem correct.
They show only that the particular change did not close this gate.
Do not merge the timer revert or a broad PR on the strength of these results.

The HTTP run's host timeline is retained in `http-origin-host.log`:

| Elapsed host seconds | Observation |
| ---: | --- |
| 209.439 | NewSession starts |
| 405.099 | NewSession returns |
| 406.219 | GetWindowHandles returns |
| 409.726 | Navigate returns |
| 409.730 | Focus ExecuteScript starts |
| 410.035 | Owned HTTP page request returns 200 |
| 803.159 | Guest emits timeout failure before READY |

The override and its SHA-256 are retained in
`http_origin_experiment.py` and
`qemu-physical-graphics-http-origin/origin-experiment.json`.
The result remains `passed=false`, `physical=false`, with no interaction cycles.

## Historical comparison limits

The September 5 direct Firefox pass is retained in the
`megrez-desktop-only` worktree at
`target/firefox-basic-direct-capabilities-final2-20260905/result.json`.
It used SMP=4, 2 GiB RAM, kernel `c0b2eef1…`,
rootfs `b3e34491…`, and Firefox ESR 140.14 rather than the current 140.15.
Its first read-only fixture probes returned,
including transient `about:blank` and incomplete-document observations.

There is also an important QEMU configuration difference:
the passing browser gate used `cache=writeback` for its disposable root run-copy,
whereas the physical-interaction adapter inherits `cache=directsync`
from the desktop adapter.
The dedicated browser adapter already implements the writeback selection
in `browser_web_qemu_argv`, introduced by commit `d96cf4121`.
This difference warrants a single-variable experiment,
not an assumption that a kernel PR caused the regression.

The passing historical serial log also contains the `glxtest` failure
and delayed `WaitFlushedEvent` warning seen in the current run.
Those warnings alone do not identify the cause of the content-command timeout.

## Kernel patch acceptance boundary

Commit `6f47cf1da` moves user-buffer fault work ahead of socket I/O
to address the observed atomic-context page-fault failure.
Its C regression is
`test/initramfs/src/regression/network/tcp_user_buffer_prefault.c`:
a loopback TCP transfer into an untouched anonymous writable mapping,
with small receive chunks and exact payload validation.
That test's existence is not proof of every socket's Linux compatibility.

The current syscall-level prefault approach covers more than TCP.
Before treating it as an upstream-ready fix,
validate short transfers, inaccessible unused buffer tails,
EOF/nonblocking error precedence, and concurrent mapping changes.
Prefaulting a mapping is not the same as pinning it against later changes.
Keep this follow-up separate from claims about a successful browser or board run.

## Content boundary result

`content_boundary_experiment.py` inserted read-only probes before the original
focus operation, without changing the kernel or rootfs:
constant JavaScript before navigation,
constant JavaScript after navigation,
read-only document state,
then a constant in the named sandbox.
The original focus operation and all interaction assertions remain in place.
The run in `qemu-physical-graphics-content-boundary` failed closed:

| Elapsed host seconds | Observation |
| ---: | --- |
| 418.713 | NewSession returns |
| 418.901 | Pre-navigation `return 1` starts |
| 422.337 | Pre-navigation script returns `{"value":1}` |
| 448.633 | Navigate returns |
| 448.635 | Post-navigation `return 1` starts |
| 803.269 | Post-navigation script times out |
| 803.280 | Failure framebuffer captured |
| 809.991 | Bounded process snapshot completes |

The DOM-read, named-sandbox, and original-focus requests were never reached.
The pre-navigation result demonstrates that basic script execution and
Marionette transport work before navigating to the interaction page.
It does not prove that the new page's content actor or internal autofocus works.

The retained 1280x1024 failure framebuffer shows a Firefox New Tab window
with blank content, not the interaction page.
Its run-local filename is `diagnostic-failure.ppm`;
a lossless host PNG preview is `content-boundary-failure.png`.
CPU registers and the bounded process listing are retained in
`diagnostic-registers.json` and `physical-graphics-qemu.serial.log`.
Do not use the guest `ps` percentage/time values as performance measurements:
several reported values exceed the run's wall-clock bounds.

## Cache-mode result

`disk_cache_experiment.py` changes only the disposable
QEMU root run-copy from directsync to writeback.
It does not change the immutable input disk, use `cache=unsafe`,
or weaken trusted-input and screenshot checks.
No outcome is claimed for an experiment until its bounded run completes.

The run in `qemu-physical-graphics-writeback` failed closed before READY.
NewSession returned at 390.207 host seconds,
Navigate returned at 398.407 seconds,
and the original focus ExecuteScript timed out at 825.365 seconds.
The kernel and all seven other immutable inputs match the frozen baseline.
The actual QEMU arguments confirm `cache=writeback` on the disposable root.
Changing the cache mode alone did not close the first-script boundary.

## Historical-kernel control result

The exact historical passing kernel has also been recovered read-only from
the retained September 5 `boot.ext4`, rather than rebuilt from a moving branch.
Its local path is `inputs/historical-passing-kernel.Image`,
and its SHA-256 matches the historical result:
`c0b2eef1a87ded1a7c8f820337d54188c6df1946d5673ef6c39735546d322a2d`.
It is a diagnostic control only, not a replacement for the current-main target.
`historical_kernel_control.py` checks all seven non-kernel input hashes
against the failed writeback run before launching QEMU.
It then uses the same writeback adapter, SMP=4, page, and original guest gate.
Its comparison changes the kernel image only;
the result directory is `qemu-physical-graphics-historical-kernel-control`.

The historical-kernel control did not pass, but changed the observed failure:
NewSession returned at 354.789 host seconds,
Navigate returned at 356.539 seconds,
and the focus ExecuteScript returned at 375.068 seconds.
The guest then failed with `physical-graphics-input-focus`, not a command timeout.
The unmodified guest did not print the raw focus result,
so this run alone does not distinguish null, missing input, or unsuccessful focus.
Its framebuffer still showed New Tab with blank content.

This narrows the investigation without establishing a working old-kernel gate.
In the matched comparison, the historical kernel allowed the first content command
to return; the current kernel did not return within its bounded setup window.
More than one kernel change differs, so this does not identify a particular PR.

## Document readiness correction

The guest now checks the exact expected document URL and `readyState=complete`
before trying to focus the input.
It retries only the known transient null response or an explicit loading result,
without resetting the client's original setup deadline.
Missing input in a completed document, failed focus, transport errors,
and exhausted setup time still fail closed before READY.
Trusted events, nonce matching, screenshots, and the three-cycle requirement
are unchanged.

Three new regression tests failed before implementation and passed afterward.
The combined guest/host/QEMU interaction unit suites passed 75 tests,
and five actual JavaScript evaluations covered wrong URL, loading document,
missing input, unsuccessful focus, and successful focus.
These host checks do not substitute for the pending guest runtime verification.

The readiness overlay was generated offline through the persistent launcher
in 5.945 seconds, at `readiness-rootfs/`.
Its root image SHA-256 is
`f59baa0bc73a7a0529b0b7d317369fd530c16d96c0ef359706de1ac0c048bdec`,
and its manifest SHA-256 is
`4a8c9882ae04cd51ca75d14354e81348245203172ad03d33854b497d47f3a2ba`.
The package lock and checksums remain unchanged.
Comparing the two overlay manifests shows exactly one changed payload:
`/usr/lib/asterinas/physical-graphics-gate`, now SHA-256
`1661f4d1f8d8b999bf516fca29c11aa2821be447501d9c64e6f99dd0674dcff4`.
The other 16 overlay payloads are identical.

Runtime verification with the historical control kernel is in
`qemu-physical-graphics-historical-readiness`.
The original current-kernel and rootfs inputs above remain untouched.

The control reached READY at 428.162 host seconds:
the first focus probe returned a transient response,
the second returned successfully at 416.757 seconds,
and FullscreenWindow returned at 428.159 seconds.
At 438.294 seconds the shell reported cycle 1 exit status 1.
The retained traceback identifies the first snapshot ExecuteScript call,
which raised `browser_m5_marionette_gate.GateError`.
The exception escaped the guest's handler because its own `GateError`
is a different class. No standard FAIL marker was emitted.
The host was interrupted with SIGINT after observing this terminal failure,
rather than waiting out the remaining PASS timeout.
The canonical result remains `passed=false`, `physical=false`, with no cycles.

## Terminal-failure handling correction

The guest now catches its imported Marionette transport error explicitly,
including connection, setup, snapshot, and final-verification failures.
It emits one FAIL marker and closes already-acquired resources.
The QEMU and physical-board cycle loops now reject a command-status marker
received before the required READY/PASS sequence completes.
Even status 0 cannot substitute for missing interaction evidence;
malformed terminal statuses also fail immediately.

New tests reproduced the escaping exception and the extra read after exit
before implementation. The combined guest/host/QEMU suites then passed
78 tests in 2.229 seconds. These are host contract tests, not board acceptance.

The snapshot error was investigated separately.
`snapshot_sandbox_experiment.py` retains the original named-sandbox call,
enables detailed Marionette error logging, and on a protocol error retries
the identical read-only script without the named sandbox parameters.
It does not change the HTML, synthesize DOM events, or bypass nonce,
trusted-event, evdev, or screenshot checks.
Its runtime override is diagnostic only and must not be confused with
an unmodified production-gate pass.

## Snapshot realm and tablet corrections

The sandbox diagnostic reached READY at 323.416 host seconds.
At 325.340 seconds the original named-sandbox call returned
`TypeError: window.__asterinasPhysicalGraphicsSnapshot is not a function`.
At 325.716 seconds the identical script without `sandbox` and `newSandbox`
returned a valid page snapshot. This is a same-session comparison,
not a kernel or Firefox package change.
The production snapshot request now omits those two parameters.
Its exact read-only script and the post-READY command whitelist remain enforced.
A new regression rejects script, argument, and sandbox parameter substitutions.

The diagnostic still timed out at 623.427 seconds with no completed cycle.
Its retained framebuffer, previewed as `snapshot-sandbox-failure.png`, shows
the complete 16-character nonce, `trustedKey=true`, `trustedInput=true`,
but `trustedPointer=false`, `trustedClick=false`, and an amber button.
This establishes visible keyboard progress on the historical control,
not a pointer, current-kernel, or physical-board pass.

The host adapter had two independent pointer defects:

- [QEMU 10.2.1 HMP mouse movement](https://github.com/qemu/qemu/blob/v10.2.1/ui/ui-hmp-cmds.c)
  emits relative events, while the
  [VirtIO tablet handler](https://github.com/qemu/qemu/blob/v10.2.1/hw/input/virtio-input-hid.c)
  accepts absolute motion and buttons. The existing HMP motion was therefore
  not delivered to the tablet.
- The fixed pixel target `(640,600)` is below the button in the retained
  1280x1024 fullscreen page; the button spans approximately y=428..575.

The adapter retains the same VirtIO devices and sends keys through HMP,
but uses a private QMP socket for bounded absolute motion and a single click.
Targets start at `(640,500)` and move 32 pixels right per cycle,
inside the same button. Distinct endpoints also avoid losing motion evidence
if the browser coalesces motion across consecutive cycles.
Socket-level tests check exact ABS/button messages, acknowledgement failure,
deadline expiry, invalid coordinates/timeouts, and connection closure.
This does not introduce browser-side input synthesis.

The corrected guest was installed in `witness-rootfs/` through the persistent
offline launcher in 5.637 seconds:

| Input | SHA-256 |
| --- | --- |
| Root image | `76c57bfbd74ed1a45cc387408e2334efed5d1e2b184c77bce941ff31e603318a` |
| Manifest | `a7e6cc9c191f12f73ea74756cc94a99df78f02fa0e397fc95c4f99431b051315` |

Package lock and package checksums still match the frozen baseline.
The next historical-kernel runtime uses this packaged guest without the
sandbox diagnostic override, at `qemu-physical-graphics-witness-historical`.
The writeback run-copy control remains explicit in its QEMU arguments.

That historical run reached READY at 345.368 seconds, but timed out at
645.376 seconds without a completed cycle. A live framebuffer showed a full
nonce and trusted keyboard/input state, but no trusted pointer/click state.
The pointer's location alone did not demonstrate that it had moved.
Two additional diagnostic QMP motion-only requests, `(20000,20000)` and
`(17000,16000)` separated by one second, were issued after the original
single click. No additional click or browser-side event was synthesized.
The before/after framebuffer hashes were identical:
`1ec030ed757854d45411a6947831e3becc209c644b39376f20ba96622bed7ae3`.
QMP `query-mice` reported the current device as an absolute QEMU Virtio Tablet.
These extra movements make this run diagnostic rather than an untouched
three-cycle acceptance attempt; its canonical result is false in either case.

The retained run-root's `/home/asterinas/Xorg.0.log` identifies `event0` as
the keyboard and `event1` as the pointer. Xorg detects both absolute axes,
configures the latter as a touchscreen, and states that relative axes are
ignored. This narrows the remaining pointer investigation, but does not yet
locate the loss in VirtIO, evdev, Xorg, or Firefox.
The current kernel was then tested against the same corrected root at
`qemu-physical-graphics-witness-current`.

The current-kernel run subsequently failed before READY at 849.872 host
seconds. NewSession completed at 457.646 seconds, Navigate at 464.843 seconds,
and the first guarded focus script did not return before the setup deadline.
Its live and failure framebuffers show a blank New Tab window.
The run used the original `5689ba87…` current kernel and `76c57bfb…` witness
root, with no sandbox retry override or extra motion injection.
The corrected witness did not resolve the current-kernel content boundary.
The result is `passed=false`, `physical=false`, with no interaction cycles.

The smaller diagnostic, `raw_tablet_experiment.py`, masks Firefox entirely and
observes the same current kernel through evdev and read-only XQueryPointer.
It reads device names, event capabilities, absolute axis calibration, and raw
input records around one key and one QMP tablet click.
It deliberately cannot publish browser or physical acceptance.
This separates input delivery from the slow browser/content-process boundary.

## Current-kernel raw input result

`qemu-raw-tablet-current/raw-input-diagnostic.json` records a successful
bounded raw-input observation at 124.163 host seconds, followed by guest
diagnostic exit status 0 at 124.334 seconds.
The current kernel and witness root hashes are unchanged.
Both tablet axes report the expected range 0..32767.
The exact 14-event sequence was verified programmatically:

1. `event0`: A down, SYN_REPORT, A up, SYN_REPORT.
2. `event1`: ABS_X=0, ABS_Y=0, SYN_REPORT.
3. `event1`: ABS_X=16396, ABS_Y=16015, SYN_REPORT.
4. `event1`: BTN_LEFT down, SYN_REPORT, BTN_LEFT up, SYN_REPORT.

Read-only XQueryPointer reports `(640,512)` before injection and `(640,500)`
afterward, with a released button mask. The driver SHA-256 was also checked
against the retained source. QEMU and the runner exited; no guest was left
running and the persistent container was retained.

This verifies one current-kernel VirtIO-to-evdev-to-X11 input transaction,
including packet ordering, absolute calibration, and released-button state.
It does not cover real USB xHCI/HID, three Firefox cycles, or HDMI scanout.
The enclosing graphics gate intentionally remains `passed=false` and
`physical=false`: this diagnostic does not execute its browser acceptance
protocol. Its runner exit status 1 is therefore expected, not a claim that
the observed raw transaction failed.

The next kernel experiment should target the current/historical browser
content-command difference, keeping this input result separate.
The TCP-local fault-handling candidate is commit `161a8e679`;
compare it in isolation with the current syscall-wide prefault commit
`6f47cf1da`, including partial-transfer and cold-buffer regressions.
No such kernel change has been integrated on the strength of these diagnostics.

## Physical lifetime review

The scoped code review confirmed that independently renewed
setup/cycle budgets could exceed the unchanged 900-second board reboot timer.
The correction preserves that safety timer and caps all guest phases at a
shared host deadline, measured conservatively before booti with 30 seconds
of headroom. Exhaustion fails closed and enters the existing recovery path.
A simulated-time regression covers two 350-second cycles and rejects a third
that would cross the board lifetime; it failed before the fix.

The combined guest, physical-host, QEMU adapter, and QMP suites passed
107 tests in 2.708 seconds with ResourceWarning promoted to an error.
The scoped review and its verified resolution are retained under
`review-readiness-failures.md`. These checks do not establish board acceptance.
