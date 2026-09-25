# Bounded Megrez Firefox load, 2026-09-25

The board completed a short, higher-load desktop test and returned to RockOS under serial control. This run used the installed Asterinas Debian image and a previously verified kernel generation; it does **not** qualify an uninstalled build from current `main`.

## Admission and recovery

The stable serial device was `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`. The selected boot artifacts on RockOS `/boot` passed read-only SHA-256 and U-Boot CRC checks before boot:

| Artifact | SHA-256 | CRC32 |
| --- | --- | --- |
| `asterinas-ptrace-6694c4c7ff5aeb71.booti` | `6694c4c7ff5aeb715c5c3acf9a9c2f0f6318eaba7d7848a61b9d88bc63333180` | `ab15b580` |
| `stage1-cfec77d41d68.cpio` | `cfec77d41d68dfcb67228dfa6791d9cea25227c427ee9943ba5f44d6bd269cd1` | `1f97beff` |
| `megrez-465cb129333c.dtb` | `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba` | `40ed4c65` |

The [recovery gate](recovery-serial.log.gz), [load boot](load-serial.log.gz), and [Asterinas handoff](reboot-serial.log.gz) serial transcripts are retained as compressed logs. RockOS `/dev/mmcblk1p2` was unmounted and a read-only `e2fsck -fn` returned 0. The boot entry selected the firmware 1920×1080 framebuffer and the opt-in serial root debug console. No boot menu or image was rewritten. The preliminary gate armed `ASTERINAS_SAFE_REBOOT_AFTER=300` and `asterinas.reboot_after=420`; the serial stream proved Asterinas root UID 0, systemd PID 1, ext2 `/dev/mmcblk0p2`, and active desktop services, followed by a fresh OpenSBI/U-Boot epoch. RockOS was booted and its root UID 0 plus boot ID were verified again after closing and reopening the host serial connection.

The load boot armed userspace sync/reboot at 900 seconds and a kernel fallback at 1140 seconds. Its boot ID was `66f1a216-ff18-47d8-8621-959aa8b5f98c`; serial probes again verified root UID 0 and active desktop. At Asterinas uptime 724 seconds, a nonce-framed UID 0/boot-ID probe succeeded. `sync; reboot -f` returned to a new U-Boot epoch. RockOS was then booted; a new root UID 0 and boot ID `7ad6e45a-88ab-4307-9726-dd0437c3d378` were independently verified after closing and reopening serial. The board was left in RockOS.

## Browser start issue

On this installed image, the first Firefox start failed because the persisted `/home/asterinas/browser-web-timeline.log` was owned by root, so UID 1000 could not append to it. A later restart also failed its `asterinas-desktop-m5-network.service` requirement under the direct-network test configuration. The live run changed only the file owner and started `asterinas-browser-web.service` with `--job-mode=ignore-dependencies`; Firefox then stayed active. These are image/startup-path findings, not benchmark failures. Current source differs from this installed generation, so reproduce against a newly built image before changing `main`.

## Official Speedometer 3.1

The board loaded the pinned [Speedometer 3.1](https://github.com/WebKit/Speedometer) checkout `1386415be8fef2f6b6bbdbe1828872471c5d802a` from the host's local HTTP server. The official `iterationCount=1&startAutomatically` query ran all 58 subtests in Firefox 143.0.3. The benchmark reached its valid summary and displayed **0.4127**. [The machine-readable result](speedometer-result.json) records the exact URL and version. One iteration is a bounded functional/performance baseline, **not** the default ten-iteration score or a Linux comparison.

During the run, `ps` showed Firefox Web Content around 70–89% CPU and the parent Firefox process around 45–50% CPU. Available memory stayed above 9.3 GiB of 10.45 GiB. This shows substantial browser CPU use but does not separate user-space rendering from kernel time. The Asterinas image exposed no `thermal_zone*/temp` or `cpufreq/scaling_cur_freq` entries, so temperature and throttling cannot be inferred from this run.

## Repeated 720p playback

After Speedometer, the same Firefox/Xorg session played the existing 10-second, 1280×720, 30 fps VP8 clip three times at native 1280×720 layout. The source SHA-256 was `1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`. All three visible plays reached the `ended` event without a board lockup. The saved [run 1](video1.json), [run 2](video2.json), and [run 3](video3.json) preserve playback quality counters, process CPU ticks, and hot Firefox threads.

| Run | Dropped / total frames | Playback wall time | Firefox CPU ticks | Xorg CPU ticks | Hottest Firefox threads |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 123 / 300 | 10.38 s | 2009 | 56 | Renderer 825; SwComposite 741 |
| 2 | 133 / 300 | 10.50 s | 1930 | 63 | Renderer 818; SwComposite 760 |
| 3 | 138 / 300 | 10.58 s | 1981 | 45 | Renderer 787; SwComposite 725 |

The drop range is close to [earlier same-clip native-video measurements](../2026-09-24-megrez-browser-performance/README.md), which found 124–143 dropped frames in several runs. A recent Speedometer run did not produce a new hang or clearly worsen playback, but native 720p remains visibly below acceptable fluidity. The CPU accounting points toward Firefox rendering/compositing as the next *measurement target*; it does not prove the kernel is uninvolved. The next kernel investigation should sample user versus system CPU time, page faults, scheduler waits and framebuffer writes for these same bounded workloads before selecting a kernel optimization.
