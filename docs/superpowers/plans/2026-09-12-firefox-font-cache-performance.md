# Firefox font-cache performance investigation

## Scope and result

The font-cache defect is fixed in the rootfs builder and repaired on the board.
This does **not** establish an end-to-end Firefox startup improvement. A full
Desktop boot still takes several minutes to produce a visible browser window.
Power-cycle research is deferred by the user; it is not a prerequisite for
this software investigation.

The board used the existing qualified four-mode menu, kernel
`485b9079c204bf6b34055f5e1061f3011381557d8cc4b4bf2d1e4831922058c1`, and Stage1
`d62ab8325e03ec9959a82336ad7ddab2d433d9e6dfc1006d6237fa9f6c80c1e0` unchanged.
The kernel is the frozen, previously tested integration artifact, not a new
build of this branch. Debian runs unmodified Firefox ESR 140.15.0, BuildID
20260826142222. The existing volatile-home/profile policy was retained.
There was no kernel build, image replacement, container recreation, firmware
environment edit, or network-stack merge.

Local raw evidence is under `target/firefox-perf-20260912/`; generated logs and
images are not committed. Browser runtime logs on this installed root are
under `/run/asterinas-browser-web/`, not the paths of older wrapper revisions.

## Root cause: FONT-CACHE-001

Two independent defects made the prebuilt system font caches unusable:

1. All installed `*-le64.cache-9` files were owned by root with mode 0600.
   The builder's private `umask 077` leaked into target cache generation.
   Firefox and Openbox run as UID 1000 and could not read these files.
2. Cache headers recorded build-time font-directory mtimes, such as
   `1788811158.429648535`. The builder subsequently normalized directories to
   `1704067200.0`. Fontconfig checks both seconds and nanoseconds, so merely
   making these caches readable still caused rescanning.

The old checker accepted the four-byte cache magic without checking either
condition. The production finalizer also bypassed an existing audited helper,
repeated scans, and could create an explicit-QEMU placeholder with that magic.
Consequently, a cache-ready marker did not prove a usable cache.

The fix normalizes font-tree timestamps before scanning, uses a scoped 022
umask only for target `fc-cache`, and calls the audited helper once. A failed
scan remains fatal even if old cache files exist. The checker now verifies
public readability, the 64-bit little-endian v9 header, directory containment,
and exact directory mtime. It is a header/identity check, not a complete parser
for every serialized font record. Target Fontconfig runtime checks provide
the separate usability evidence below.

## Controlled component experiment

All three observations used UID 1000 and a different, initially unused HOME
in the same Asterinas boot. `FC_DEBUG=16` records actual cache writes and mtime
comparisons. The readable and valid variants used a temporary `/run` overlay;
the original persistent caches were not changed during this A/B experiment.
Page caches warm across this sequence, so these are component observations,
not independent cold-boot benchmarks.

| System-cache variant | `fc-match` real time | Cache-directory rewrites |
| --- | ---: | ---: |
| Original unreadable, stale caches | 15.436 s | 22 |
| Same stale caches, now readable | 13.886 s | 22 |
| Readable caches with matching timestamps | 2.176 s | 0 |

Evidence: `font-ab.log`, `font-readable.log`, and `font-fresh.log`. The last
log includes the real 64-byte cache header and Fontconfig's explicit timestamp
mismatch messages. Permission repair alone was insufficient; matching
timestamps removed the rebuilds. This does not mean Firefox became 86% faster.

## Persistent repair and Desktop observation

After normal software recovery to RockOS, the unmounted Debian partition was
mounted for this scoped cache repair only. Original caches were archived on
RockOS at `/var/tmp/asterinas-font-cache.N5P112/before.tar`, SHA-256
`05f6b0e882617a50081ce85a8ccaa1e5f3135720adb8022a1615dd7caef5d9c5`.
Target `fc-cache -f -v` regenerated them with readable permissions. A fresh
UID-1000 query in the Debian chroot recorded zero rewrites. The partition was
synced and unmounted before boot. See `repair-font-cache.log` and the retained
one-off `repair-font-cache.sh`; this is not a new recurring deployment step.
Rollback, if required, must restore that archive only to the verified Debian
cache directory from RockOS while Debian is not running.

The unmodified-cache baseline (`baseline-2`) reached the root console at
menu +65.441 s and first detected a visible Firefox window at menu +289.649 s.
Queries were spaced by 30 seconds plus their execution time; this is a
detection upper bound, not an exact window-creation timestamp.

After repair (`corrected-1`), the console appeared at menu +64.654 s. A sample
at menu +106.650 s suffered serial input truncation and the host query timed
out. Reattaching later immediately produced a working root prompt and uptime;
this was not proof of a guest freeze. Because sampling was interrupted, this
run is not a clean paired startup benchmark. Subsequent queries found no
window around guest uptime 299 s and a visible window around uptime 332 s.
These guest-clock observations must not be presented as precise host-clock
speedup/regression measurements. In particular, they provide no evidence that
the remaining multi-minute startup delay is solved.

An additional UID-1000 query with a new HOME in that fresh Asterinas boot took
2.909 s and recorded **zero** font-cache rewrites. Fontconfig explicitly
reported matching `1704067200.0` directory/cache timestamps. See
`corrected-1/final-and-recovery.log`. This validates that the persistent repair
survives boot independently of the earlier RAM overlay.

The Desktop guest then returned through a single synchronized systemd reboot
and the menu's normal default to RockOS. `post-repair-check.log` confirms SSH
access, `end1` UP at `10.100.19.200/21`, and an unmounted read-only
`e2fsck -fn /dev/mmcblk1p2` exit status of zero. The active menu hash remains
`02280720efe7a1ad0ac084cdc20429406631e12d2e16f05638544bab0883fb26`, and the
original-cache backup hash still matches. No operator reset was needed.

The first baseline harness attempt (`baseline-1`) separately waited for two
menu markers that arrived in one read. The second wait timed out after the
first had consumed both. The menu safely defaulted to RockOS; this attempt is
neither a Desktop boot result nor an Asterinas failure. Subsequent selection
waits for `Enter choice:` once and checks the returned firmware/menu buffer.

## Regression coverage

Both the unreadable-cache case and the normalization-before-scan case failed
against the prior implementation before their fixes were applied. Tests also
cover stale nanoseconds, a magic-only placeholder, empty successful scans,
failed scans with old cache files, and preservation of host umask 077.

The persistent development container passed 64 browser contract tests plus
314 rootfs, builder-image, boot-menu, debug, serial-session and DTB tests.
The initial broader invocation omitted `tools/riscv` from `PYTHONPATH`, causing
two import errors; the corrected invocation passed. These are native/unit
tests, not 378 QEMU boots. Shell syntax, Python fatal-error lint, and whitespace
checks are also required before commit. No fresh full rootfs build or QEMU
boot is claimed for this cache-only change.

## Remaining performance work

Do not add repeated full Desktop boots or verbose global syscall traces as
the next diagnostic. There are two concrete boundaries to address:

- **Make CPU measurements trustworthy.** Current proc task `stat` emits
  `read_jiffies()` and start-time values at internal `TIMER_FREQ=1000`, while
  the board's `getconf CLK_TCK` reports 100. The ELF loader does not publish
  `AT_CLKTCK`. Thus `ps` CPU-time formatting is not a reliable duration here.
  Fix the user-visible unit contract separately, with proc/auxv/CPU-clock
  consistency tests; do not change the scheduler's internal timer frequency.
  Page-fault counts are currently zero placeholders, not evidence of no faults.
- **Split pre-window kernel work from browser work.** Use short, identical
  binary/file-loading and memory-mapping probes on RockOS and Asterinas, with
  explicitly warm/cold cache conditions and per-phase wall/CPU time. The
  Firefox process still consumes substantial reported kernel time after the
  font repair. That observation alone does not distinguish page-fault I/O,
  VM work, contention, or scheduling.

`glxtest` with the existing software-rendering environment exits with the
expected error in 3.659 s on Asterinas versus 0.179 s in the RockOS chroot.
The browser's later `ManageChildProcess failed` message is not, by itself,
evidence of a 120-second GLX wait or a fatal graphics failure. The first
RockOS `firefox --version` reference lacked `/proc` and failed, so its timing
is invalid and must not be used. Missing DRM, external networking, and mouse
latency were not causally tested by this experiment.
