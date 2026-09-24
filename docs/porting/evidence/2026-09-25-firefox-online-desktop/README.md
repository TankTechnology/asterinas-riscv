# Firefox online desktop and short Megrez video baseline

Date: 2026-09-25.  This record separates the signed QEMU image from a
temporary, memory-only desktop experiment on the previously installed Megrez
system.  The latter does **not** establish that the new image survives a board
reboot.

## Signed QEMU desktop

The RISC-V Sv39/SMP=4 kernel Image was
`a64142bd775fd46c958125d9a85256bd45e96d984d47695e09327bb496dbc01b`.
The signed `browser-web` ext2 root was
`b7f0f75a8bc2e6fcc2e60e2058ebcfbb1ec926b633b6bca0f55710d618e9bab2`.
The root contract verified against its manifest and package lock.  The short
graphical gate completed in 80.03 seconds with six captured states: Firefox
open, minimized, restored, Files, Terminal, and a taskbar switch to Firefox.
The [minimized capture](qemu-minimized.png) visibly shows the wallpaper,
desktop launchers, and bottom taskbar.  Files and Terminal windows and the
taskbar switch were also visually inspected in the retained local gate output
`/tmp/asterinas-main-desktop-shell-qemu-final/`.

The signed image contains a world-readable shared MIME database.  This is
necessary for PCManFM to recognize `.desktop` launchers and the SVG wallpaper;
the first signed attempt without generated MIME data displayed blank icons and
no wallpaper.  The panel uses application IDs instead of absolute launcher
paths, and the Terminal launcher uses one quoted-free Xterm title argument.
The gate used a QEMU tablet and framebuffer.  It does not exercise Megrez HDMI
scanout or physical USB input.

The broader online browser gate did **not** pass on this signed image.  It
twice reached Baidu's home page, then stalled after Marionette requested the
controlled fixture home page.  The first run was stopped after about eight
minutes; the second bounded diagnostic run ended with
`browser-timeout:phase-probe-fixture-home`.  The fixture received twenty
earlier network-probe requests but no request for
`/browser-quality/index.html`.  Twelve GDB-stub samples during the second
stall showed active user-space PCs and idle kernel harts, without recurrence
of the earlier vDSO/coarse-clock lock deadlock.  They do not identify the
Firefox function or prove that the kernel is uninvolved.  The local raw
samples are under
`/home/ubuntu/.codex/asterinas-main-qemu-pc-20260925/`.

A third bounded QEMU run tested whether the new desktop processes caused this
stall.  A verified development overlay replaced only
`/usr/lib/asterinas/desktop-m5-session` with the pre-shell version; it kept
the same signed root as its base and the same Sv39 kernel.  PCManFM and LXPanel
therefore did not start.  The [raw gate result](qemu-shell-off-result.json)
still failed at `browser-timeout:phase-probe-fixture-home` after Baidu loaded.
The fixture again served twenty network-probe requests and received no browser
workload request.  This excludes the new shell processes as a necessary cause
of this particular QEMU failure, but does not identify the remaining cause.

## Megrez temporary desktop

The board boot ID was `c6846443-9d90-4ff5-854e-5c4132fcfbbc`; the selected
kernel file remained `/boot/asterinas-route-052656e9b12c.booti` with SHA-256
`052656e9b12ce1586a76e18c75acece62c748bb3e6c9430d764fd0d7436ff116`.
Two nonce-framed serial connections, including a close and reopen, proved UID
0, PID 1 `systemd`, and `/dev/mmcblk0p2` mounted as ext2.  The serial device
was `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.

The older installed root had PCManFM, LXPanel, and `update-mime-database`, but
no `/usr/share/mime/mime.cache`, desktop profile, or `Desktop` directory.  A
SHA-256-checked bundle staged the new wallpaper, launchers, and profiles under
`/run/asterinas-desktop` and the existing tmpfs-mounted
`/home/asterinas`; MIME data was generated under `/run`.  No MMC partition or
boot menu was written.  The [open capture](megrez-open.png) shows wallpaper,
desktop icons, Firefox, and the bottom panel.  The Files and Terminal panel
buttons launched real windows; the [Terminal capture](megrez-terminal.png)
shows a shell prompt, and the taskbar switched back to Firefox.

The physical minimize check is **not accepted**.  After minimizing Firefox,
PCManFM stayed running and its wallpaper and the panel remained visible, but
the desktop icons disappeared in the
[captured frame](megrez-minimized-icons-missing.png).  Restoring Firefox or
restarting PCManFM brought them back.  The loss repeated after restarting
Firefox while PCManFM was already running, so launch order alone does not
explain it.  This live-overlay result must be repeated on a boot of the signed
image before assigning the defect to Asterinas, Xorg/fbdev, or PCManFM.

## Bounded video measurement

The [raw run record](physical-video-runs.json) uses the same
[300-frame VP8 clip](../2026-09-24-board-video-current-image/result.json) as
the preceding board probe (SHA-256
`1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`).
The board display was 1920×1080.  Each browser run played ten seconds of the
1280×720 source; only its display size changed.  The runs alternated large
and small to limit drift, with no long stress interval.

| Displayed video | Dropped frames per 300 | Median | Firefox parent CPU ticks, user + kernel |
| --- | --- | ---: | --- |
| 1280×720 | 112, 118, 116 | 116 | 2438, 2490, 2368 |
| 640×360 | 11, 15, 19 | 15 | 1942, 1621, 2108 |

`getconf CLK_TCK` was 100.  The parent Firefox process used roughly 23.7–24.9
CPU-seconds in the full-size runs; Xorg used 0.86–0.97 CPU-seconds.  Firefox
content processes were not included in these process counters.  The full-size
loss is close to the prior one-run 121/300 observation, but these are
different boots and do not qualify an A/B speedup or a no-regression claim.
The size sensitivity points to video presentation work as a priority for
function-level profiling; it does not by itself isolate decode, scaling,
compositing, X11 copy, scanout, or a kernel cost.

The new Sv39 kernel and signed root were not installed on Megrez for this
record.  Reboot persistence, the seven-group physical browser gate, the
minimize defect, and a qualified performance A/B remain open.  The DRM branch
was not merged.

A separate Sv48/SMP=4 release Image was built for a later controlled Megrez
boot: SHA-256
`4d1e0ee4ef2cb23ae6bbbca3ec425b3177013d5bdf1c261bc2e990ade22c9025`.
It was not installed or booted during this record.
