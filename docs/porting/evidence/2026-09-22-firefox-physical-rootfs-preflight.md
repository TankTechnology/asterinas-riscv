# Firefox physical startup: root filesystem preflight

## Scope and control

The user explicitly deferred KVM and requested physical Firefox/Bilibili work.
The preflight started in RockOS `6.6.87`, boot identity
`6e0c2402-86cf-4fb6-99f4-385bbdc8543d`.
Fresh SSH and authenticated UART root commands succeeded.
After explicit user approval, the offline repair and network publication below
were performed, followed by a software transition to a bounded Asterinas boot.
The persistent default selector was not changed.

The intended next boot uses the frozen, previously tested `gp`-fix kernel,
an explicit serial debug console and the existing bounded desktop workflow.
New boot artifacts belong on partition 3, not the full `/boot` partition.
The board's most recent retained desktop generation `c0efdf35ee6b5543`
records expected root SHA-256
`302b50941781e8bcda3174d91da65e334abddda0d2009fcea6aa2a5f5f640057`,
matching the local `browser-web-dns2` manifest.
This is expected base identity, not a claim that the used filesystem is byte-identical.
The board also contains the Firefox 143.0.3 JIT-overlay marker;
presence of that marker does not identify which browser executable a future service will select.

## Blocking physical evidence

`/dev/mmcblk1p2` is unmounted and has size 4,294,967,296 bytes.
Its ext2 filesystem label is `ASTER_BROWSERWEB`, UUID
`c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10`, with 524,288 blocks of 4096 bytes.
Although the superblock says `clean`, a full read-only check disagrees:

```text
e2fsck -fn /dev/mmcblk1p2
FSCK_RC=12
```

The checker reports zero-length directories, deleted inodes with zero deletion time,
and malformed extended attributes for inode 19686.
It aborts while reading the extended-attribute block and warns that errors remain.
The separate future-timestamp warning reflects RockOS's clock being behind the host;
it does not explain the illegal attribute header or directory errors.

Read-only `debugfs ncheck` maps these affected inodes:

| Inode | Observed pathname |
| ---: | --- |
| 19686 | `/var/lib/systemd/random-seed` |
| 20100 | `/home/asterinas/.mozilla/firefox/ojfc8kjg.default-esr/broadcast-listeners.json` |
| 20039 | `/home/asterinas/.cache/mesa_shader_cache_db/part40/mesa_cache.idx` |
| 20060 | `/home/asterinas/.cache/mesa_shader_cache_db/part47/mesa_cache.idx` |

Inode 19686 reports a 32-byte file, data block 309979 and attribute block 282231.
Other reported empty directory inodes were not resolved to paths by this check.
No corrupted object was deleted or rewritten during this read-only preflight.
The evidence does not establish when the corruption occurred or whether its cause
was a reset, filesystem implementation defect, concurrency defect or another mechanism.

The local frozen base image separately passed all five phases of
`e2fsck -fn`, exit 0, in the persistent development container.
This distinguishes the observed used-board state from the clean local base;
it is not an end-to-end validation of the board's storage driver.

## Prepared artifacts

- Frozen kernel SHA-256:
  `ae905fc0f4b78a10175729931ca0d717f2d6a31f5595898b074f74f72d25e8e2`.
- Rebuilt Stage1 with the serial-getty retention guard, 1,102,848 bytes:
  `476ba95b99ecf89c8cf53956c7df683975cdf34b61b6403e5bb3ec81d0d335cf`.
  The ordinary cross container lacked cross-libc headers;
  the existing persistent `riscv-rootfs` image built it successfully.
- The four targeted suites passed all 120 tests in 7.264 seconds.
- A generic `megrez_debug plan` attempt rejected the fast path's 300-second
  timeout as too short for its distinct browser-qualification contract.
  That attempt published no plan and no deadline validation was weakened.
  A separate `plan-fast.json` uses the existing fast desktop contract and exact
  artifact identities; it is not a browser preboard permit.

## Preservation and next action

A full raw backup was completed from the unmounted partition to the fresh directory
`/home/debian/asterinas/p2-recovery-20260922-m80mt2pz/` on RockOS partition 3.
The backup script requires the exact source size and host-root partition,
refuses an existing destination, compares source/backup SHA-256 values,
and makes the completed image read-only.
It has no write operation against the source device.
Both the original partition and the 4 GiB `original.img` returned SHA-256
`8521b906af95f96c99a1b3a0f51b1ccb588cf0ae73166a157c05c950903928a7`.
The script reported `P2_BACKUP_VERIFIED` and `BACKUP_RC=0`.
The original backup is read-only and must be retained through any filesystem repair.

Offline repair was explicitly submitted for user approval because it may clear
corrupted files or attributes.
The user approved continuation and reiterated that large transfers should use
RockOS and Ethernet rather than UART.

## Approved repair and network publication

The repair script rechecked the partition identity, size, unmounted state,
RockOS root partition, and preserved backup metadata before writing.
`e2fsck -fy /dev/mmcblk1p2` returned 1 (errors corrected).
After `sync`, an independent `e2fsck -fn` completed all five phases with exit 0.
The script reported `P2_READONLY_RECHECK_PASS` and `REPAIR_SCRIPT_RC=0`.

Repairs cleared invalid directory entries and the malformed random-seed attribute,
fixed link counts and bitmaps, and reconnected some orphaned files.
Block 283183 was multiply claimed by `/var/backups/dpkg.status` (inode 19710)
and the old Firefox profile's `storage.sqlite` (inode 19885).
The checker resolved that conflict, but structural consistency does not prove
application-data integrity or establish the cause of corruption.
The original, hash-verified 4 GiB backup remains read-only on partition 3.
The selected development boot uses a fresh volatile home instead of the damaged
old Firefox profile.

The new kernel, Stage1, DTB and publication script were transferred over Ethernet
and SHA-256 verified on RockOS.
Generation `cd89b13808aff3397dc11175e2b2ac75c098a3b7a2081686597131dd3b5283b4`
was published under `/home/debian/asterinas/boot/cd89b13808aff339`.
The full `/boot` partition, persistent menu and U-Boot environment were not changed.
The temporary artifact server was closed after successful publication.

Authenticated UART root issued `sync; systemctl reboot` once.
The host observed fresh firmware, interrupted the U-Boot countdown,
and loaded all three partition-3 artifacts with exact size and CRC32 checks.
The bounded desktop workflow observed kernel entry, root handoff and the
`ASTERINAS_DEBUG_CONSOLE_READY uid=0` marker.
This marker alone was not accepted as a verified reconnectable command channel.
The first bounded start failed its visible-Firefox-window check and recovered
to a fresh U-Boot prompt in 364.999 seconds including loading and recovery.
The final readiness reason was `firefox-window`, not root mounting or Xorg startup.
`start-1/result.json` records failure, not desktop admission.

RockOS was then booted and authenticated without physical reset.
Its boot identity was `6afdc1ef-af52-49a1-ac3d-a13ba4018989`.
Fresh UART root reconnect and SSH probes succeeded.
The unmounted partition again passed all five read-only filesystem-check phases,
exit 0; its inode/block counts matched the repaired state.
There is no persistent `/var/log/journal` on this image.
The installed browser wrapper's SHA-256 matches the current source:
`fd1d44546d56a40558f9f68d2525d562495bdc0288e7242d4a26bc69efba8749`.

## Same-artifact diagnostic boot

One additional boot with identical artifacts and arguments collected live
observations before the watchdog expired; no configuration A/B sweep was used.
The Asterinas boot identity was `790121bb-fe96-4b1d-ae56-830ca2bc7139`.
Fresh nonce responses proved UID 0, then the UART was closed, reopened,
and a second fresh nonce proved UID 0 with the same boot identity.
An earlier diagnostic parser rejected a valid response because a kernel log
prefix shared its line; the subsequent fresh framed probes supplied the evidence.

Inside this Asterinas boot, `getent ahostsv4 www.baidu.com` returned addresses
and exit 0 with `/etc/resolv.conf` pointing at `127.0.0.1`.
Bounded `curl` requests through `10.100.19.216:17893` returned HTTP 200 for both
`https://www.baidu.com/` and `https://www.bilibili.com/`, without bypassing TLS checks.
This proves guest resolver/proxy connectivity, not Firefox page rendering or video.

Firefox service PID 191 was `/usr/lib/firefox/firefox`, with zero restarts.
The snapshots showed continuing startup work:

| Guest uptime (s) | CPU ticks | Threads | RSS (KiB) | Visible windows |
| ---: | ---: | ---: | ---: | ---: |
| 145.71 | 2258 | 3 | 30592 | 0 |
| 200.55 | 6933 | 25 | 94372 | 0 |
| 259.56 | 12954 | 47 | 140368 | 0 |

These are sequential, non-atomic observations, not a throughput benchmark.
The actual guest lacks `/proc/191/io`, `/proc/191/syscall` and `/proc/net/tcp`;
zero-valued fallback fields in the helper are not measured zero I/O or switches.
Firefox eventually logged `Marionette ... Listening on port 2828`, alongside
GLX child-process, pixbuf and missing accessibility-DBus warnings.
The scheduler also logged `Switching to a task already running in the foreground`,
including a short burst around kernel timestamp 222 s.
Progress during these samples does not establish whole-kernel liveness or explain
every warning; no visible-window admission or video-playback result was obtained.
The 300-second watchdog expired before the next root query could execute.
Fresh firmware and U-Boot were captured; the planned `sync; reboot -f` command
was not sent, so this is watchdog recovery rather than a command-reboot result.

## Build-profile discrepancy and optimized candidate

The frozen `gp`-fix kernel's retained build log explicitly says
`Finished dev profile [unoptimized + debuginfo]`.
The existing September 12 startup investigation already measured a substantial
dev/release startup difference on an earlier source revision.
The build profile should have been checked before applying the fast desktop budget;
the current timeout must not be presented as proof of a kernel deadlock.
The historical measurements do not quantify this new revision's speedup.

The clean isolated source commit `3aa196979` was rebuilt through the persistent
container with `RELEASE=1`, unchanged SMP=4 and `riscv_sv39_mode`.
The build completed in 41.61 seconds with 12 existing kernel warnings.
The optimized production artifacts were frozen before kernel tests could replace
the default build output:

- Image: 6,051,080 bytes, SHA-256
  `54d8b57b7a5d97883fb353328c04702ec088adbf2c8bce61d52e29948b79b794`,
  CRC32 `6711c699`.
- ELF: SHA-256 `737e8b796e6809d2188112794223aad88f870765368af77061379b4b015da55b`.
- Preserved bundle metadata: all three build profiles are `release`.

Both exact trap-return tests passed under `cargo osdk test --release`, SMP=4:
`kernel_trap_return_preserves_current_cpu_gp` and
`user_trap_return_restores_user_gp`, each with one executed test and runner completion.
Logs are under `.worktrees/megrez-playback-v2/target/riscv-trap-gp/release-ktest.xxr3ZjdW/`.
The first host checker stopped after its guest passed because the container lacks
`rg`; the saved first result was rechecked with `grep`, then the second test ran.
No zero-test or failed-host-checker result was counted as a passing guest test.
## Optimized physical startup and Baidu rendering

After the second watchdog recovery, RockOS boot identity
`639e6ce7-b6d0-4437-9b0b-a6689cea21b7` again supplied authenticated control.
The unmounted partition passed a third post-repair read-only check, exit 0.
The optimized generation
`b4f37c30d0d67e2a6fc7810137aa3d07f82178b7b6e105062272d27d8b6df4e6`
was published over Ethernet under `/home/debian/asterinas/boot/b4f37c30d0d67e2a`.
The Stage1, DTB, base-root contract and boot arguments were unchanged.
Persistent boot selectors were still not changed.

`release-start/result.json` reports `status=pass`, `reason=desktop-ready`:

- From the host's `booti` epoch: root handoff 2.912 s,
  root console marker 6.777 s, and visible Firefox/desktop readiness 65.026 s.
- Total workflow including U-Boot preparation and the admission probe: 105.805 s.
- Asterinas boot identity: `65fd742a-60cc-4dd2-9d0c-9491776bb978`.
- Firefox PID 158, UID 1000, one visible window and an X11 socket.
- Kernel and guest watchdog-disarm markers both observed; readback 0.
- Fresh root nonce probes before and after closing/reopening UART both passed.

The unoptimized boots did not reach the visible-window deadline;
the optimized candidate did.
These few runs establish this startup outcome, not a distributional speedup
or proof that scheduler warnings and all other concurrency defects are fixed.

Before public-page navigation, the basic-only Firefox service was stopped
successfully (MainPID 0, inactive), and its temporary profile was preserved as
`/home/asterinas/.mozilla/basic-only-preserved-20260922`.
The systemd manager was set to `ASTERINAS_BROWSER_WEB_BASIC_ONLY=0`.
Restarted Firefox PID 1138 used a fresh profile whose `user.js` retained the
proxy and enabled RDD but contained no Safe Browsing disabling preferences.
No TLS bypass or sandbox-disable switch was added.

The existing `baidu-home` gate then passed on the physical board:
real navigation to `https://www.baidu.com/`, DOM/JavaScript readiness,
certificate-verification capability and framebuffer evidence.
The command returned `BAIDU_GATE_RC=0`; PNG upload returned `UPLOAD_RC=0`.
The PNG was sent over Ethernet, not UART, and visually inspected on the host.
It shows the Baidu homepage and Firefox window, not a network error page.
Screenshot: 1920x1080, 118,488 bytes, SHA-256
`a49b24d897df8e45ab3b02b81909e4aa611964e9b189827af92193ba5a6a77b4`.
The running Firefox service had zero restarts after its explicit configuration restart.
Bilibili video playback was checked separately; HTTP 200 alone was not accepted.

## Bilibili observations, failed gate and current control limitation

The existing `bilibili-playback` gate, with Firefox PID 1138 unchanged,
loaded the homepage and selected detail page `BV1TceU6iEoH`.
Both DOM readiness phases and the homepage screenshot phase passed.
Its muted-playback probe observed a blob-backed video, `paused=0`,
`ready_state=4`, and media time progressing from 0 to at least 63.953 seconds.
However, its saved promise state remained `pending`, and both event counters
remained zero throughout those samples.
The strict gate consequently returned `BILI_GATE_RC=2` after its playback window.
Its terminal message, "video element remained unavailable", does not accurately
describe the sampled video element; the preceding samples are retained.
This failed gate must not be relabeled as a pass.

A separate framebuffer capture succeeded and was uploaded over Ethernet.
The visually inspected 1920x1080 PNG shows an actual video image on the selected
Bilibili detail page, rather than a loading placeholder or just its homepage.
It is 935,409 bytes, SHA-256
`fdb7a4b3e77d29a7e2f18b81cf0a380754e7422a5b679255238300e3da8c6733`.
Together, the image and advancing media clock establish observed video rendering
and playback progress, but not sustained frame rate, decoded-frame counts,
long-duration stability or a passing automated playback contract.

There is a concrete test-harness hypothesis to verify before changing the kernel:
`_playback_probe` uses `newSandbox=True` on every sample, while its JavaScript
expects `video.__asterinasPlayback`, promise callbacks and event counts to survive
between samples.
Mozilla documents that fresh script sandboxes do not preserve globals and that
`new_sandbox=False` is required for retained script state
([Marionette API](https://firefox-source-docs.mozilla.org/python/marionette_driver.html)).
That documented contract supports the lifecycle mismatch hypothesis;
it is not a completed on-board proof of the DOM-wrapper expando behavior.
No gate predicate was weakened and no production code was changed to force a pass.

The planned independent frame-count and sandbox-retention probe did not execute
to a confirmed result: its UART query produced no normal echo or completion marker,
and the bounded transport retry also failed.
A subsequent, separately attached short fresh-nonce command likewise received no
response within 15 seconds.
Therefore the earlier successful root/reconnect checks do not establish that
control remained usable at the end of the playback attempt.

Host checks also found an external network change:
`enp12s0` still had carrier but no IPv4 address, and NetworkManager reported
repeated wired 802.1X association/`no-secrets` failures.
`ip route get 10.100.19.200` now selected Wi-Fi through `10.100.79.1`
with source `10.100.79.6`, not the tested `10.100.19.216` wired path.
Thus the failed ping/SSH attempts and stalled proxy stream cannot be attributed
to Asterinas alone.
This host-network fault does not explain the independent UART failure.
No campus-network credentials or authentication policy were changed.

The UART device remained present at its stable by-id path, with raw 115200 settings
and no other process holding it after each probe.
No fresh panic trace or firmware epoch was observed in these failed queries.
A single `Ctrl-C` followed by `sync; reboot -f` was then sent as a bounded
software-recovery attempt.
The 75-second observation expired without fresh output or a U-Boot prompt,
as recorded in `post-playback-software-recovery.log`.
Command execution and reboot are therefore unconfirmed.
The host serial descriptor was then closed; no serial owner remained.
The user was asked only whether the visible screen was still advancing,
not to type commands or perform a physical reset.
The user subsequently confirmed that the Bilibili video had visibly played,
with some stutter that they considered acceptable for this stage.
They then observed a stopped image with a buffering message displaying `1.31 MB/s`
and offered a physical reset if needed.
This is independent human confirmation of earlier playback, not measured frame rate.
The displayed transfer rate is not evidence of current network throughput,
and buffering alone does not establish a kernel-wide hang.
After this report, host inspection still found no wired IPv4 address and no competing UART owner.
A single-owner reset listener was armed for 20 minutes at approximately 20:55 CST.
It waits for fresh OpenSBI/U-Boot before selecting RockOS,
then uses the existing authenticated login flow and a fresh root/boot-identity probe.
The listener does not itself reset the board or change persistent boot selection.
Its transcript is `post-playback-reset-takeover.log`;
being armed is not evidence of a completed reset or recovered control.
Temporary artifact/upload servers on ports 18080 and 18081 have closed.
The original partition backup and both immutable kernel generations are retained.

## Recovery after the user reset

At approximately 20:56 CST, the armed listener captured fresh OpenSBI/U-Boot,
selected the unchanged vendor RockOS entry and completed authenticated serial login.
RockOS reports Linux `6.6.87` and boot identity
`7fc45632-f952-4dd1-b3b7-6c7646af958e`.
A fresh nonce response established UID 0.
After closing and reopening the host serial descriptor,
another nonce response established UID 0 with the same boot identity.
The board-side root shell remains available and no host process retains the UART.

The Asterinas partition was unmounted.
`e2fsck -fn /dev/mmcblk1p2` completed all five phases with
`POST_RESET_FSCK_RC=0`, with the same 20,046 files and 292,736 used blocks.
No repair or other filesystem write was performed by this check.
The original backup still has size 4,294,967,296 bytes, owner root and mode `0400`;
this is a presence/metadata check, not a new full-image hash verification.
`/sys/fs/pstore` was absent on this RockOS boot,
so this inspection recovered no persistent crash record.

RockOS `end1` has `10.100.19.200/21` and its SSH service is active,
but end-to-end SSH remains unavailable.
The host still has no wired IPv4 address and selects its Wi-Fi route to the board.
Board probes to the former host address, configured gateway and host Wi-Fi address failed;
the gateway neighbor remained unresolved.
Host SSH with existing credentials and strict host-key checking timed out.
An attempt to activate the existing wired profile was rejected by NetworkManager authorization;
noninteractive sudo also required a password.
No credential, authentication policy, host route or interface configuration was modified.
Physical topology still needs confirmation before prescribing an authenticated campus
connection versus an isolated board-management link.

### Failure ordering and code-level leads

The retained unsuccessful UART probe was last written at 20:36:49 CST.
Host NetworkManager first records loss of the wired lease at 20:39:27 in the
inspected 20:20–20:50 interval, then `ip-config-unavailable` at 20:40:16,
followed by repeated authentication failures.
These timestamps do not establish the exact onset of either underlying fault,
but they do not support explaining the earlier UART failure by the later recorded
host-network teardown.

The frozen release boot log contains several
`Switching to a task already running in the foreground` warnings,
and another appears during the Bilibili query.
In the exact booted source revision `3aa196979a4330b78a2453ddb567e24b4d85a5a1`,
`ostd/src/task/processor.rs::before_switching_to` emits this warning in an
IRQ-disabled compare-exchange loop on `switched_to_cpu`.
`after_switching_to` releases the previous task's flag after the context switch.
This identifies a concrete wait/release path to audit for progress,
but finite contention is possible and the warnings alone do not prove deadlock.
No task/CPU ownership snapshot or terminal crash stack was captured.
Host-side UART logging was per operation, not uninterrupted full-lifetime capture;
the previous guest's volatile `/run` evidence cannot be assumed to survive reset.

The immediate follow-up order is to establish the host-board network configuration,
audit the serial receive/wakeup and scheduler wait/release paths,
verify the separate playback-sampler state-retention hypothesis,
then run one instrumented physical playback validation with continuous capture.
Broad parameter sweeps or another unchanged browser boot would not resolve these gaps.

### Durable local evidence bundle

The existing text logs, manifests, scripts and both uploaded screenshots were also
archived outside `target/` as
[`2026-09-22-firefox-physical-logs.tar.gz`](2026-09-22-firefox-physical-logs.tar.gz).
This 1,229,428-byte archive has SHA-256
`f06716086446271d75fbbe06c81cdfb29ca35928bc212d7baaad4ac14d9b9024`.
It is local and uncommitted, not a remote backup.
It preserves already captured evidence, not the missing terminal crash state.

## Raw evidence

Local evidence directory:
`target/megrez-debug/firefox-physical-20260922-m80mt2pz/`.

- `rockos-inventory-nopager.log`: authenticated root inventory and retained manifest.
- `rootfs-readonly-check.log`: complete failing read-only filesystem check.
- `rootfs-damage-scope.log`: partition geometry and inode/path mapping.
- `frozen-rootfs-readonly-check.log`: successful local frozen-image check.
- `backup-rootfs.sh`, `rootfs-backup.log`: exact preservation script and live execution.
- `repair-rootfs.sh`, `rootfs-repair.log`: guarded approved repair and read-only recheck.
- `published-manifest.json`, `publication.serial.log`: immutable network publication.
- `rockos-to-uboot.log`: authenticated software reboot and fresh firmware capture.
- `start-1/`: first bounded startup failure and proven automatic recovery.
- `post-boot-rootfs-check.log`: clean post-boot filesystem and root reconnect.
- `diagnostic-root-2.log`, `diagnostic-root-3.log`: fresh Asterinas root/reconnect probes.
- `diagnostic-browser.log`, `diagnostic-network.log`, `diagnostic-late.log`: live observations.
- `diagnostic-return-rockos.log`: watchdog recovery during a late query.
- `release-build.log`, `release-artifacts/`: optimized build and preserved identities.
- `release-start/`, `release-start-live.log`: successful optimized physical admission.
- `release-root-0.log`, `release-root-1.log`: optimized root/reconnect probes.
- `public-browser-start.log`: normal-public configuration and preserved test profile.
- `baidu-gate-progress-1.log`, `bilibili-gate-launch.log`: Baidu success and upload status.
- `baidu-web/`: inspected physical screenshot and network-upload identity.
- `bilibili-progress-1.log` through `bilibili-progress-3.log`: live-page and playback samples.
- `bilibili-capture.log`: failed gate terminal status and successful framebuffer upload.
- `bilibili-web/`: inspected physical video screenshot and upload identity.
- `bilibili-independent.log`, `post-playback-control.log`: incomplete/no-response probes.
- `post-playback-software-recovery.log`: single bounded software-recovery attempt.
- `post-playback-reset-takeover.log`: fresh-firmware listener and conditional RockOS recovery.
- `post-reset-reconnect-fsck.log`: verified RockOS root reconnect and clean read-only partition check.
- `post-reset-network-paths.log`: failed existing gateway and host Wi-Fi paths from RockOS.
- `stage1/initramfs.cpio`: newly built and published Stage1.

The first inventory command entered `debugfs`'s pager and timed out without disk changes.
The next attachment quit that observed pager with `q` and set `PAGER=cat` before continuing.
That host-command timeout was not a board hang or a successful inventory result.
