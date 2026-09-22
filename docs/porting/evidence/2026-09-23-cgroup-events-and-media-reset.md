# Cgroup notifications, desktop shutdown, and a short media reset test

A missing cgroup notification kept systemd waiting after every desktop process
had already exited.
The kernel fix makes the desktop service reach `inactive/dead` by the first
approximately one-second observation after the stop command.
A separate real-Firefox experiment handles a reset media connection,
recovers through an explicit retry, and exits normally.
Neither result establishes the cause of the physical-board playback/UART hang.

## Desktop shutdown: evidence and fix

Both desktop runs use QEMU RISC-V virt, four CPUs, 2 GiB RAM, TCG,
and the same immutable Debian browser root image.
Each run receives its own writable disk copy.
The original service has `KillMode=control-group`, `KillSignal=15`,
`SendSIGKILL=yes`, and a configured 90-second stop timeout.
These settings are unchanged by the fix.

| Observation | Original release (`stop-01`) | Patched kernel (`stop-green`) |
| --- | --- | --- |
| Desktop initially running | Five processes; `populated 1` | Five processes; `populated 1` |
| First snapshot after one-second sleep | `deactivating/final-sigterm` | `inactive/dead`, `Result=success` |
| Snapshot after another three seconds | Still `deactivating/final-sigterm` | Still `inactive/dead` |
| Desktop processes remaining | None; `MainPID=0`, empty `cgroup.procs`, `populated 0` | None; `MainPID=0`, service cgroup removed |
| Root serial witnesses | 18 fresh successful commands | 33 fresh successful commands |

The patched stop command returned at guest experiment time 0.248 seconds;
the first completed status snapshot was at 1.323 seconds.
Thus completion was observed 1.075 seconds after command return;
this is a sampling upper bound, not an exact shutdown duration.
The original run stopped collecting after about five seconds.
Its host `passed=true` means diagnostic collection completed,
not that desktop shutdown passed or that this run waited the full 90 seconds.
The patched script additionally asserts `inactive/dead` and `MainPID=0`.

A separate original-kernel reproducer (`notify-red`) opened the events files
and pinned inotify watches through `/proc/self/fd/`.
A pipe-controlled child entered and exited a leaf cgroup.
Both leaf and parent changed `populated` between zero and one,
but neither transition produced `IN_MODIFY` within the bounded observation.
Normal repeated path watches also returned different watch descriptors.
The C regression's original-kernel run (`regression-red`) independently failed
both repeated-path/mount-alias identity checks and notification collection.

[Systemd v257 watches `cgroup.events` with `IN_MODIFY`](https://raw.githubusercontent.com/systemd/systemd/v257/src/core/cgroup.c).
[The cgroup v2 interface specifies notifications for populated transitions](https://cdn.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html#un-populated-notification).
The observed empty cgroup, absent notifications, and successful patched shutdown
support a missed empty-cgroup notification as the cause of this desktop delay.

The fix gives each cgroup's fixed `cgroup.events` attribute a canonical weak
inode cache, shared across path lookups and cgroupfs mount aliases.
It publishes `IN_MODIFY` for nodes whose recursive population changes 0 ↔ 1,
including affected ancestors.
Counter propagation remains under the existing node lock;
strong references retain changed nodes until notifications publish after unlocking.
Deleting the cgroup retires its events watch once and disables new subscriptions.
Lookups that create an inode after deletion keep it private until retirement.
Other dynamically controlled cgroup attributes retain their revalidation policy.

This implements direct events-file inotify notifications.
It does not implement parent-directory watch notifications or
`poll`/`epoll` priority notifications on the attribute.

## Minimal regression and checks

`test/initramfs/src/regression/process/cgroup_events.c` is registered in the
existing process regression runner and automatically built by its Makefile.
It uses two pipe-controlled children and checks:

- Stable watch identity after repeated reads/lookups and through a second mount.
- Leaf and ancestor notifications on first entry and last exit.
- An ancestor remaining populated while its other child cgroup still has a process.
- Explicit watch removal, implicit removal on cgroup deletion, and new identity
  plus notifications after recreating the same path.

Each expected notification batch has a single 250-millisecond deadline.
The test collects expected watches across multiple reads,
because child and ancestor events may arrive separately.
The QEMU wrapper also gives the complete C program an eight-second deadline.
No long-running load is part of this regression.
The original failing C revision used a single poll/drain;
the final revision handles separate delivery without weakening expected events.
Both revisions' delivered programs are retained in the raw archive.

The patched QEMU run passes the C program with zero failures and the desktop
shutdown assertion in the same boot.
The final source compiled with the normal `-D__asterinas__ -Wall -Werror -O2`
produces exactly the same binary that was executed in the passing guest:
`f7aa09c8a7fac775aeea24dda964a7571c748b33912467efa74f6bc32cf7987f` (SHA256).
The RISC-V release kernel build, targeted rustfmt check,
and C clang-format check pass.
The full cross-architecture CI matrix was not run.
Existing unrelated kernel build warnings remain in the archived build log.

A native Linux 6.5.0-15-generic control confirms watch identity and all four
population-transition batches, plus explicit watch removal.
It does not pass the full test: implicit events-file deletion notification is
absent at both 250 milliseconds and a diagnostic two-second bound.
The native debug log identifies that exact failing phase.
[Linux v6.5 kernfs removal](https://raw.githubusercontent.com/torvalds/linux/v6.5/fs/kernfs/dir.c)
lacks the inode-link clearing found in
[current kernfs removal](https://raw.githubusercontent.com/torvalds/linux/master/fs/kernfs/dir.c),
which explicitly enables VFS deletion notifications.
We retain the stricter Asterinas deletion regression and record this older-Linux
control limitation rather than increasing the normal test duration.

## Media reset, retry, and exit

`media-reset-02` uses the original release kernel and Firefox 143.0.3,
with the corrected production `_playback_probe` from the preceding change.
A guest loopback HTTP server supplies the repository's one-second silent VP8 clip
through a real MediaSource.
After playback advances beyond 0.25 seconds, the server closes the second
HTTP/1.1 response with zero socket linger, after sending only 32 of 646 bytes.
The fixture page handles the fetch error, exposes a retry button,
and constructs a new MediaSource when retry is requested.

| Phase | Guest experiment time | Observed outcome |
| --- | ---: | --- |
| Partial response reset | 17.839 s | Declared 646 bytes; sent 32 |
| Error UI and DOM response checked | 18.531 s | Network error shown; media waiting; DOM control responds |
| Explicit retry accepted | 20.203 s | Video time 1.389533 s; seven decoded frames |
| Firefox quit response | 23.670 s | `forced=false`, `in_app=true` |
| Browser PID 243 disappears | 25.591 s | Normal process exit observed |
| Experiment complete | 25.720 s | Guest exit zero; host result passes |

Time includes Marionette session creation and excludes QEMU/desktop startup.
There are 58 unique successful nonce-framed UID 0 serial responses,
all from boot `492ed099-cd92-41d4-b4f5-a6813278c680`.
The quit uses
[Marionette's `eAttemptQuit` command](https://raw.githubusercontent.com/mozilla/gecko-dev/master/remote/marionette/driver.sys.mjs),
without a forced process kill.

The earlier `media-close-01` variant used an HTTP/1.0 response and an orderly
short close; Firefox did not produce the fetch error required by that fixture.
Its assertion failed after the bounded observation, without a system hang.
This is retained as a fault-injection expectation mismatch,
not evidence of a new kernel failure.
The reset variant makes the intended interruption explicit.

These are fixture-page error and retry controls, not changes to Bilibili's UI.
The traffic is guest loopback TCP; it does not exercise a physical NIC,
external CDN, hardware video decoder, or the board's DW-APB UART.
DOM clicks are not physical keyboard/mouse verification.

## Timeout evidence and replay

The archived runner continuously saves raw serial bytes.
On a guest failure or short experiment timeout, it attempts an HMP capture:
pause QEMU, record all four CPU PCs/registers and IRQ information,
and resume in a `finally` block before normal teardown.
Matching kernel ELF files provide address symbolization.
Capture failure is recorded separately and preserves the original failure.
The same capture runs at the desktop observation boundary.
The original stopped-desktop snapshot has three idle CPUs and one trap entry;
it does not provide complete blocked-thread stacks or prove absence of a race.

The adjacent `2026-09-23-cgroup-events-and-media-reset/` directory contains
normalized observations, results, a raw log/script archive, and SHA256 indexes.
Normalization removes byte-identical repeated `LIFECYCLE` lines only.
Individual run results contain complete input hashes and QEMU arguments.

- Original Image: `54d8b57b7a5d97883fb353328c04702ec088adbf2c8bce61d52e29948b79b794`.
- Patched Image: `05bc7a85538ab246d96ab87ea67f22d767dbfc93fa5a895e9a6f1c19d9418ad0`.
- Common root image: `bd855c9855e6734cb5e154d488d7c1899f9c1949ac01dae87cd0fc88702e35cf`.
- Patched boot: `b2540a31-1463-43e2-aae8-163690b11291`.
- Patch base: `d67e5f708538121644d2ce2cc64ddc21797f5177`.

Replay requires the original local artifacts and persistent container layout;
disk images and kernel binaries are not included in the archive.
Restore its top-level runner and guest scripts under
`/root/asterinas/target/qemu-lifecycle-20260923/` in the main dev container.
The runner reads `target/qemu-stability-20260922/browser-release-plan.json`.
Create fresh output directories, then run from the checked-out worktree:

```sh
PYTHONPATH=. ASTERINAS_TEST_KERNEL=/path/to/kernel.Image \
  python3 /root/asterinas/target/qemu-lifecycle-20260923/run.py \
  stop-with-regression stop-replay
PYTHONPATH=. python3 /root/asterinas/target/qemu-lifecycle-20260923/run.py \
  media media-replay
```

Keep a matching `kernel.elf` beside the selected Image for symbolization.
The archived regression guest embeds the tested binary;
regenerate its base64 payload after rebuilding the C regression to test changes.
The desktop experiment phase has a 45-second host bound;
media has a 160-second bound including Marionette session setup,
with separate startup limits and 12-second serial command limits.
These are failure bounds, not requested workload durations.
All test QEMU processes were torn down; no physical-board experiment was run.
