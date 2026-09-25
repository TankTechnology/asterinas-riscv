# Browser startup and kernel-log follow-up (2026-09-25)

This is a short, simulation-only follow-up to the [Megrez browser diagnostic
wave](../2026-09-25-browser-full-diagnostics/README.md). The board was not
rebooted or modified; its last verified state remains RockOS. The four QEMU
runs used the same U-Boot, DTB, Stage1, Debian package lock, and browser-web
base image. Each used four RISC-V CPUs, Sv39, 2 GiB of guest RAM, and the
opt-in root debug console. The gate verifies UID 0, systemd on ext2, active
desktop and graphical targets, network fixture requests, and a rendered
screenshot. The exact input hashes and QEMU command line are recorded in each
`result.json`.

The [probe](probe.py) collected `systemctl --failed`, browser status, the
timeline's owner, the full available boot journal, dmesg, and write/read
checks for `/proc/sys/kernel/pid_max`. Both log collectors exited zero on
every run. The guest ran `sync` before the QEMU snapshot was inspected.

| Run | Change under test | Gate | `systemd-sysctl` | Desktop first attempt | Scheduler warning | Journald early miss |
| --- | --- | --- | --- | --- | ---: | ---: |
| [warning-only](warning-only/result.json) | One warning per scheduler handoff | pass | failed | succeeded | 1 | 121 |
| [sysctl-before-link](sysctl-before-link/result.json) | Writable `pid_max` | pass | succeeded | failed once; recovered on restart | 1 | 141 |
| [link-before-ring](link-before-ring/result.json) | Atomic input-link replacement | pass | succeeded | succeeded | 1 | 121 |
| [final](final/result.json) | 512-record kernel log ring | pass | succeeded | succeeded | 1 | 0 |

The final kernel SHA-256 is
`33a08c3f67d34843cda4e11b8ab6eab0cab811a7d9e3de4a4dd6b72089d23004`;
the final disposable root-image SHA-256 is
`931dd9b7d776e2a009f561fa55cd02dc072eb2951de93fa59a87c6553603f035`.
The final [browser timeline](final/browser-web-timeline.log) records Firefox
exec at 6.28 s and Marionette readiness at 15.64 s. These are boot markers,
not an A/B performance score: startup varies across the short QEMU runs.

The [earlier physical journal](../2026-09-25-browser-full-diagnostics/README.md)
had 1,012 scheduler warnings, mostly in four bursts, because the warning was
inside a failed compare-exchange spin loop. The new code preserves the atomic
handoff and emits at most one warning per waiting episode. The QEMU result
confirms boot functionality and one observed warning; it is not a physical
before/after count. The earlier 256-record kernel ring also lost 121–141 early
boot messages before journald could drain them. With 512 records the final
[journal](final/wave4-journal.log) reports no missed kernel messages or
`/dev/kmsg` overrun in this boot.

The [sysctl probe](final/wave4-state.txt) reads the Debian-requested default
4,194,304, writes and reads 32,768 successfully, rejects 300 with `EINVAL`
without changing the value, then restores 4,194,304. The allocator's limit
and cursor now change under the same lock. The transient desktop failure in
the intermediate run was `ln: ... /run/asterinas-input/keyboard: File exists`:
the desktop and browser services both call the setup script. The final script
creates private links and atomically replaces the public names, so concurrent
setup no longer has an unlink/create window. This also prevents the observed
two-second service restart from being needed in the final boot. The fact that
the old `flock` did not prevent this one collision warrants a separate focused
test; these runs do not prove a general `flock` defect.

The only failed unit in the final [`systemctl --failed` snapshot](final/wave4-state.txt)
is the DNS shim, which lacks its proxy host in this direct-network QEMU
configuration. It did not block the browser or the network fixture. The
previously measured 720p video hotspot is Firefox's software YUV presentation;
this follow-up did not run video playback and makes no claim of a playback or
2× speedup. A short physical comparison with the same 720p source and frame
drop counters remains necessary before attributing a user-visible gain to any
of these changes.

Validation of the final source: the release RISC-V Sv39 SMP4 kernel build
passed; `cargo fmt --all --check`, `bash -n` on the device setup script, and
`git diff --check` passed; the standalone kernel-log store suite passed all 14
tests. The final QEMU gate passed with zero journal/dmesg collector errors.
