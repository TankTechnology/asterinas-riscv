# Megrez Firefox video profile and scheduler nice repair (2026-09-29)

## Result

The physical Firefox desktop still spends most video time in its software
renderer and compositor. Repairing `setpriority` gives fair/batch tasks the
requested CPU weight, but **does not measurably accelerate the tested 720p
video**. It is a useful contention-control primitive, not a substitute for
the unfinished GPU rendering path.

All measurements below are short, scripted experiments. The displayed pixels
were not captured externally, so this evidence proves Firefox's internal video
counters, CPU use, boot, and desktop service health, not physical HDMI quality.

## Bottleneck evidence

The same hash-pinned 10-second, 300-frame VP8 clip was played by Firefox 143
through its Marionette interface. The source and display sizes matched. The
clip SHA-256 is `1aa863f2a73698241c9daa016c7bfcfa0f94b038ca9f0b29f296c3ca0b65eb95`.
The guest probe recorded `getVideoPlaybackQuality()` and per-thread
`/proc/<pid>/task/*/schedstat` before and after playback. Probe and serial
transcripts are in [the evidence directory](evidence/2026-09-29-megrez-firefox-perf/).
The second boot reused the `large3`/`small2` run names, so the optional host
metrics upload returned HTTP 409; the guest's direct Marionette reads and
serial summaries succeeded. The second-boot raw host metrics JSON was not
retained.

| Image | 1280×720 dropped / 300 | 640×360 dropped / 300 |
| --- | ---: | ---: |
| Previous kernel (`4009be3ba` image) | 167, 118, 126 | 9, 13 |
| Repaired kernel (`7bf48c198854` image) | 148 | 12 |

On the previous kernel's paired 720p/360p run, Firefox `Renderer` and
`SwComposite` used 17.56 s combined CPU run time at 720p versus 10.39 s at
360p. Xorg changed only from 0.45 s to 0.58 s. On the new kernel's paired run,
the same two Firefox threads used 15.81 s versus 10.65 s; Xorg used 0.66 s
versus 0.47 s. Thus the resolution-sensitive work is chiefly in Firefox,
while Xorg's framebuffer copy is a smaller cost in this workload. A previous
PC sample placed 51 of 60 sampled `libxul` PCs in SWGL's
`linear_row_yuv<false>`; 17 of 20 matching `libc` PCs resolved to the generic
`syscall` wrapper. PC counts are location evidence, not elapsed-time shares.

The attempted A/B/A scheduling intervention raised the `Renderer` and
`SwComposite` threads from nice 0 to nice -5 via `sched_setattr`. Dropped-frame
counts were 132, 118, and 124. This is within the baseline variation, and
some wait time moved to Xorg, so it was not enabled as a Firefox default.
Without a supported GPU userspace stack, the current fbdev desktop remains a
software rendering path.

## Correctness and CPU-share repair

Before this change, `setpriority(PRIO_PROCESS, ...)` stored the process nice
value but left existing fair scheduler policies at their old weight. A
regression test on the old physical kernel observed `getpriority()==5` while
`sched_getattr().sched_nice` remained 0. The repair updates the scheduler
policy of each live fair or batch thread after storing the process nice value.
The existing process-oriented priority ABI is retained; full Linux per-thread
nice semantics are separate work.

The focused QEMU `AUTO_TEST=sched_policy` gate passed, including checks for
the calling and an already existing worker thread. The original single-thread
test also passed on the repaired physical kernel. In two 2-second same-core
CPU-share samples, measured process CPU times were:

| Kernel and phase | Foreground nice / CPU ms | Background nice / CPU ms |
| --- | ---: | ---: |
| Old, equal | 0 / 998.9 | 0 / 999.2 |
| Old, background deprioritized | 0 / 999.8 | 10 / 998.9 |
| New, equal | 0 / 996.5 | 0 / 1001.4 |
| New, background deprioritized | 0 / 1805.7 | 10 / 192.7 |

The foreground received about 1.81× its former CPU allocation in this
specific same-core contention test. This is **not** a 1.81× Firefox speedup:
the browser video trial above did not improve, and no general desktop latency
claim follows from this synthetic CPU-share probe.

## Selected physical boot and recovery

The host used `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`
with one serial owner. The old Asterinas boot ID was
`b4b58244-8a4d-480a-997c-021251298da6`. Software reboot returned to
RockOS boot ID `6ade85f4-4613-48bc-ae2e-dc7cda77a2ef` on
`/dev/mmcblk1p3`; partition 2 was unmounted. The new image was copied to a
separate directory on partition 3, without replacing the existing image or
default boot entry. U-Boot verified each selected artifact's CRC and size.

Selected artifact SHA-256 values were kernel
`7bf48c19885466a8c12797c3b544da9c5344bd69db961314d956eb93f108a444`,
Stage1 `666d58af0673e2af52c9f1f36b5438095c759d95b3efcd800180054606bb3d71`,
and DTB `465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`.
The one-shot boot reached the root debug console in 74.60 host seconds. A
newly opened serial connection proved UID 0 and boot ID
`d5e68c9c-bd7e-452d-b5d9-56c73a1574b4`. Firefox, Xorg with fbdev, and the
desktop-ready service were running. The desktop-ready marker matched that boot
ID. RockOS remains the default recovery entry; the board was left on the
repaired Asterinas desktop with the root serial console available. A later
fresh nonce-framed command at guest uptime 410 s still proved UID 0, the same
boot ID, Firefox PID 268, and the exited-successful safe-reboot service.

## Next performance decision

For 720p smooth playback, prioritize eliminating Firefox's CPU YUV conversion
and software composition through a working GPU userspace rendering path, or a
measured software fast path. Another round of blanket scheduler priority
changes is unsupported by the playback evidence. A physical HDMI capture
device or other independent pixel observer is still needed to validate actual
scanout, tearing, and frame presentation.
