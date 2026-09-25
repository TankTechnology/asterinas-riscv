# Megrez browser diagnostic wave, 2026-09-25

This run closes the earlier evidence gap: the **same bounded boot** now has
workload phase markers, Firefox/Xorg/desktop logs, the complete available boot
journal, dmesg, unit status, process CPU counters, and sampled Firefox PCs.
The unmodified guest [diagnostic archive](diagnostics.tar.gz) is the primary
record (SHA-256 `3b901e6ec8ddbe0d41812984d74fd91bbb4c68a922341bce6fe5cbecd80361b5`).
The [derived summary](summary.json) can be regenerated with
`python3 analyze.py diagnostics.tar.gz`; passing `--libxul-debug` and the
matching Firefox debug ELF additionally resolves PC symbols. The collector
(`collect-guest.sh`) and phase driver (`workload-guest.py`) are retained here;
the [CPU probe](../2026-09-25-physical-browser-cpu-breakdown/cpu-breakdown-guest.py),
[PC sampler](../../../../tools/riscv/debian/rootfs/thread_pc_sampler.py), and
[video fixture](../2026-09-24-megrez-browser-performance/video-source-perf.html)
are already tracked in the repository.

## Run contract and recovery

The installed Asterinas image used Firefox 143.0.3, Xorg/fbdev at 1920×1080,
the pinned Speedometer 3.1 checkout `1386415be8fef2f6b6bbdbe1828872471c5d802a`,
and the existing ten-second, 1280×720, 30 fps VP8 fixture. The browser test
used one Speedometer iteration, then three native-size video plays in an A/B/A
sequence, with 30 PC samples per renderer thread only in the middle play. The
same Firefox PID 3206 and Xorg PID 275 survived all phases. Each phase has
monotonic markers in the journal and `phase-events.jsonl`; all four workload
and log-collector exit codes are zero. The journal and dmesg were captured
before recovery. Some kernel messages had already been lost by journald during
startup (below), so the archive is **not** a complete kernel trace.

The serial console was `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
The selected kernel, Stage1 and DTB identities, UID-0 handoff, actual software
reboot, and reopened RockOS UID-0 probe are in [control-proof.txt](control-proof.txt).
Asterinas boot ID was `d2e3d7fd-89d1-4219-a68b-8594fb2b8f9a`; final RockOS
boot ID is `109902ec-2e5b-461d-a05c-3b1d6db867f1`. The board was left at a
verified RockOS root prompt, not in the test OS.

## Performance result

Speedometer reached its valid native summary with score **0.4348** in 116.5 s.
The Firefox tree used 149.02 s user CPU and 40.54 s system CPU; Xorg used
1.00 s total. This is a single-iteration observation, not a default score or
evidence of improvement over the prior 0.4292 run.

| 720p play | Dropped / 300 | Firefox user + system CPU | Renderer + SwComposite CPU | RDD process CPU | Xorg CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| Before sampling | 127 | 32.66 s | 15.33 s | 9.16 s | 0.58 s |
| With PC sampling | 141 | 29.96 s | 14.47 s | 9.05 s | 0.47 s |
| After sampling | 133 | 32.49 s | 15.45 s | 9.37 s | 0.51 s |

The measured playback includes navigation and probe overhead. The 60 PC
samples from `Renderer` and `SwComposite` contain 47 in the matching Firefox
`libxul.so`: **39 in `linear_row_yuv<false>`** and 8 in `linear_blit<true>`;
13 are in libc. The debug ELF Build ID was
`1c9f58ed7ccb6730d65e3957f560dc732b577218`. This independently confirms
the earlier software YUV presentation hotspot. With the prior same-source
640×360 control dropping only 7–9 frames, software presentation remains the
best-supported first target for video performance. The sampled play dropped
8–14 more frames than the two controls, so the sampler itself may have an
effect; do not use the sampled run as a performance baseline. This evidence
does not show a kernel CPU bottleneck or a measured 2× speedup.

## Issue register and disposition

| Priority | Observation and time | Evidence and interpretation | Next action |
| --- | --- | --- | --- |
| P1 | Native 720p video drops 127–141/300 frames during 294–330 s | `result.json`, `active-pcs.jsonl`, matching Firefox debug symbols. Rendering/compositing and YUV conversion dominate; Xorg uses under 0.6 CPU s/play. | Profile an uninstrumented 720p and 640×360 pair with matching symbols, then test a bounded software-conversion optimization or accelerated DRM path. Retain identical source and playback-quality counters. |
| P1 | Browser does not start automatically in this installed image | At 35.66 s, `browser-web-timeline.log` is unwritable by uid 1000; the service fails. Its installed unit still `Requires=asterinas-desktop-m5-network.service`, which fails at 4.85 and 37.87 s in this direct-network boot. Live chown and `--ignore-dependencies` were required. | Rebuild and boot current `main` image, then check timeline ownership and installed unit dependencies before testing browser startup. Current source already provisions uid-1000 ownership and no longer declares that network dependency; do not infer this source is broken from the older installed image. |
| P2 | Kernel warning flood hides startup diagnostics | `journal.log` contains 1009 scheduler-contention warnings before the workload and three more during Speedometer. Journald reports five `Missed ... kernel messages` events and three `/dev/kmsg buffer overrun` notices. | In QEMU, count distinct contention episodes versus spin-loop iterations around `ostd/src/task/processor.rs::before_switching_to`, then bound this warning without suppressing a persistent lockup. Repeat a boot log-capture gate. The warning flood is **not** aligned with the video phases. |
| P2 | `systemd-sysctl.service` fails at 3.5 s | `kernel/pid_max` rejects Debian's write of 4194304 with `Operation not supported`; `core_pattern` and `unprivileged_userns_clone` are absent but ignored. This is a kernel compatibility gap, not shown to explain playback. | Decide whether to implement writable `pid_max` semantics or scope the sysctl settings for the image; verify systemd-sysctl on QEMU before physical retest. |
| P3 | Xorg asks for login1; D-Bus activation fails at 35.12 s | The current boot has an active D-Bus system socket throughout the measurement. The online image deliberately masks `systemd-logind`, so Xorg reports a missing login1 session. The prior boot's missing system-bus socket did **not** recur. | Retest the full desktop with functional logind after its startup blocker is resolved; do not attribute the measured video drops to this one-time Xorg message. |
| P3 | Direct-network boot leaves the DNS shim and `man-db` failed | DNS shim lacks its proxy-host configuration at 5–6 s; `man-db` reports a temporary cache resource error at 35.75 s. The test uses a literal host IP, so neither invalidates this workload. | Keep direct/proxy profile units separate in a newly built image; inspect `man-db` only if it still fails on that image. |

Firefox's `glxtest: ManageChildProcess failed` is a graphics capability
annotation on the expected software path, not evidence that Firefox crashed:
all benchmark phases finished in the same process. The current desktop log has
only AT-SPI accessibility-bus warnings; the previous boot's lxpanel critical
messages did not recur here. The 131 `unsupported wait options ... WALL`
warnings occurred only while our ptrace PC sampler ran (309–315 s), so they
are a diagnostic-tool compatibility/noise issue, not a normal playback
symptom. The separate `SCM_RIGHTS` kernel warning appeared 39 times before
the workload; it warrants a resource-lifetime audit, but no leak was measured
in this run. No panic, OOM kill, Firefox exit, or playback error was observed
in the retained evidence.

The next boot gate should collect this same log set automatically and reject
missing files or nonzero collector exits before interpreting performance.
The first corrective QEMU work is the startup image mismatch and scheduler
logging diagnosis; the next physical video comparison should remain short,
with no ptrace on the timing baseline.
