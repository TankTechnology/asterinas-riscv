# Megrez Firefox CPU-time localization, 2026-09-25

This follow-up kept the [previous bounded Firefox workload](../2026-09-25-physical-bounded-browser-load/README.md) and split its CPU time into user and system mode. The board is back in RockOS with a verified serial root console. The measurements apply to the **installed** Asterinas image and the previously verified kernel and Stage1, not to an uninstalled build of current `main`.

## Admission and method

The stable serial device was `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`. On RockOS, root UID 0 and boot ID were probed, Debian `/dev/mmcblk1p2` was unmounted, read-only `e2fsck -fn` reported no errors, and the selected kernel, Stage1 and DTB SHA-256 values matched the [prior artifact table](../2026-09-25-physical-bounded-browser-load/README.md). U-Boot checked all three CRCs before boot. The bounded Asterinas boot kept the opt-in serial root console, a 900-second synchronized userspace reboot deadline and a 1140-second kernel fallback. Its boot ID was `797caabe-0aa8-4718-9449-6d93fc76e7d8`. The [boot transcript](boot-serial.log.gz), [handoff transcript](handoff-serial.log.gz), and [RockOS reopen probes](rockos-reopen-proof.txt) retain the control-channel evidence. The final RockOS boot ID was `9053b6c6-d420-45fc-928a-7a7bea210b99`, verified as UID 0 after closing and reopening serial.

The [CPU probe](cpu-breakdown-guest.py) read `/proc/<pid>/stat` before and after each phase for Firefox and its descendant processes plus Xorg. It read `/proc/<pid>/task/<tid>/stat` and `schedstat` for per-thread CPU and runnable wait time. `CLK_TCK` was 100. No tracked process exited or changed PID during any phase, and `schedstat` was available for all sampled threads. Measurements include navigation, playback and probe overhead in each phase; they are process CPU seconds, which can exceed wall seconds when several cores run concurrently. The two 12-second idle windows bracketed the official Speedometer run. The [raw phase data](cpu-phases.json) include the exact per-process, per-thread, and page result records.

The installed image again exposed the root-owned timeline file and browser/network service dependency found in the prior run. Before launching Firefox, the timeline file owner was repaired in this live boot, and the browser service was started with `--job-mode=ignore-dependencies`. This kept the measurement in the same Firefox 143.0.3 and Xorg processes. It does not establish that the latest source image has the same startup issue.

## Speedometer 3.1

The pinned WebKit Speedometer 3.1 checkout `1386415be8fef2f6b6bbdbe1828872471c5d802a` ran all 58 official subtests with `iterationCount=1&startAutomatically`. Its valid result was **0.4292** after 116.6 seconds. This is another single-iteration observation, not a default ten-iteration score or evidence of a gain over the previous 0.4127 run.

| Scope | User CPU | System CPU | System share of this scope's CPU |
| --- | ---: | ---: | ---: |
| Firefox process tree | 156.87 s | 38.82 s | 19.8% |
| Web Content process | 122.78 s | 21.80 s | 15.1% |
| Firefox parent process | 33.91 s | 16.96 s | 33.3% |
| Xorg | 0.84 s | 0.17 s | 16.8% |

The hottest content thread used 95.06 seconds user CPU and 12.67 seconds system CPU; its runnable wait was 4.34 seconds. Two style threads each consumed around 9–10 seconds CPU. The aggregate browser tree used about 1.68 CPU cores on average over the 116.6-second interval. The adjacent idle windows used 0.44 and 0.89 browser CPU seconds over about 12 seconds each, so the load signal is well above background activity.

## Same 720p source, two display sizes

The 10-second, 1280×720, 30 fps VP8 source remained unchanged. The first three plays used a 1280×720 visible `<video>` element. The [additional probe](display-size-guest.py) then ran two 640×360 plays and one final 1280×720 play in the same Firefox/Xorg session. [Its raw data](display-size-phases.json) include visible-size checks, Firefox playback-quality counters and per-thread CPU deltas. The page is the existing [video-source fixture](../2026-09-24-megrez-browser-performance/video-source-perf.html).

| Display size / run | Dropped / 300 | Browser user CPU | Browser system CPU | Renderer + SwComposite CPU | RDD decoder CPU |
| --- | ---: | ---: | ---: | ---: | ---: |
| 1280×720, 1 | 122 | 26.53 s | 5.65 s | 15.41 s | 8.34 s |
| 1280×720, 2 | 114 | 26.10 s | 4.83 s | 16.21 s | 8.19 s |
| 1280×720, 3 | 118 | 26.06 s | 4.92 s | 16.27 s | 8.03 s |
| 640×360, 1 | 9 | 21.31 s | 5.81 s | 10.44 s | 8.37 s |
| 640×360, 2 | 7 | 20.29 s | 5.31 s | 9.80 s | 8.05 s |
| 1280×720, after | 120 | 26.53 s | 5.65 s | 16.72 s | 8.32 s |

At native size, roughly 80–84% of Firefox's CPU time was user-mode work; Xorg used only about 0.5–0.6 CPU seconds per play. The smaller display reduced `Renderer` and `SwComposite` CPU by about 35–40% and dropped frames by more than 90%, while the RDD process stayed near 8 CPU seconds. Returning to native size restored both the rendering cost and dropped-frame count. This supports a size-dependent software presentation/compositing bottleneck rather than video decode or Xorg CPU as the primary cause. It does not identify the exact Firefox function, nor prove that kernel calls contribute no latency. Runnable wait is per-thread and cannot be summed as one serial stall time.

## Limits and next decision

`/proc/<pid>/stat` exposes zero `minflt` and `majflt` values in this kernel; current source explicitly labels those counters as placeholders. `/proc/<pid>/io` is absent in the installed image, so this run cannot attribute framebuffer writes or I/O bytes. Global `/proc/stat` source also carries a CPU-accounting TODO, so the process-level user/system clocks are the primary evidence here. No thermal or CPU-frequency sensor was available in the Asterinas image.

These measurements do not support treating kernel CPU time as the first route to a 2× improvement in video playback. The next focused profile should resolve Firefox `Renderer` and `SwComposite` stacks at both display sizes, then inspect the kernel crossings on their hot paths. The startup ownership/dependency issue should be reproduced with a newly built current-`main` image before a permanent image fix is proposed.
