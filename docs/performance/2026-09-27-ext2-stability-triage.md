# Ext2 stability triage for the Megrez desktop

The current writable Debian partition is ext2. A clean `e2fsck` after one
normal boot proves only that particular boot ended with consistent metadata.
It does not establish crash consistency or rule out a latent kernel bug.

## What the evidence establishes

| Date | Observation | What remains unknown |
| --- | --- | --- |
| September 15 | After an emergency kernel restart, offline `e2fsck -fn` found dangling `man-db` and `openbox` directory entries, link and bitmap mismatches. A second emergency restart produced another stale `man-db` entry. See the [physical time ledger](2026-09-15-firefox-physical-time-ledger.md). | No preboot check existed for the first run. The second run confirms new damage but cannot separate ext2's unjournaled crash window from an Asterinas implementation defect. |
| September 26, first recovery | Firefox saw `ESTALE` for `user.js`; fsck found an entry pointing to a deleted inode and an invalid HTree index. Commit `b063c147f` makes linear directory mutations clear `INDEX_DIR`. A focused QEMU test passed, and the repaired board booted Firefox. See the [recovery evidence](../porting/evidence/2026-09-26-browserweb-ext2-recovery/README.md). | The test proves the index flag behavior, not that all profile metadata writes are crash-safe. |
| September 26, DMA preflight | Another profile failure included `user.js` and `.startup-incomplete` pointing at deleted inodes, `prefs.js` pointing at a directory, and multiply-claimed blocks among Firefox files. The unmounted 4 GiB partition was imaged and hash-checked before offline repair. A subsequent clean desktop boot ended with fsck exit 0. See the [complete fsck logs](../porting/evidence/2026-09-26-megrez-powervr-dma-preflight/README.md). | Neither the PowerVR firmware copy nor the DMA preflight is established as the cause. The ext2 fault source is still open. |

The RockOS RTC lagged behind Asterinas, so a future superblock timestamp was
reported separately. That clock warning did not account for the structural
errors. `e2fsck` exit 4 means errors remain uncorrected; exit 1 means repairs
were made, and exit 0 means that check found no errors.

Ext2 has no metadata journal. An abrupt restart can expose partly completed
directory, inode, and bitmap updates; Linux's ext2 documentation identifies
this as a reason for post-crash checking. A file `fsync` alone does not
necessarily persist its parent directory entry. These facts make crash timing
a plausible explanation, not a diagnosis of the Asterinas implementation.
See the [ext2 documentation](https://docs.kernel.org/6.1/filesystems/ext2.html),
[fsync manual](https://man7.org/linux/man-pages/man2/fsync.2.html), and
[e2fsck exit-code manual](https://man7.org/linux/man-pages/man8/e2fsck.8.html).

## Storage-path finding and target behavior

The Megrez path used here is an **SDHC card**, not eMMC. Ext2's `sync()`
submits a block-device `Flush`. The September 27 change makes the MMC block
driver serialize this request with writes and poll CMD13 until the card reports
ready in transfer state, with a one-second timer bound and a finite poll cap;
card-reported errors and timeouts become I/O errors.
The former implementation returned `BioStatus::Complete` without checking the
card at all. The follow-up change reads SCR and, for cards advertising SD
extension commands, uses CMD48 to discover the performance extension and
whether its write cache is enabled. A block flush now waits for the card's
programming state, issues CMD49 Cache Flush for an enabled cache, waits for
completion, and reads the flush bit back with CMD48. Discovery or flush
errors fail the write or flush instead of being reported as success. A card
with a disabled cache needs only the programming-state check. The
[SD physical-layer specification](https://www.sdcard.org/cms/wp-content/themes/sdcard-org/dl.php?f=Part1_Physical_Layer_Simplified_Specification_Ver7.10.pdf)
requires Cache Flush before power-off when the optional cache is enabled.
This code has passed host-model tests and cross compilation. The subsequent
board boot reported an **enabled** SD write cache, and an explicit guest
`sync` returned success; this exercised the enabled-cache flush path. The
serial log does not expose a separate CMD49 completion trace, and there has
been no physical power cut.
Neither the old no-op nor CMD13 alone proves that the observed ext2 damage
came from the card. Linux's
[MMC block driver](https://github.com/torvalds/linux/blob/master/drivers/mmc/core/block.c)
and [SD card driver](https://github.com/torvalds/linux/blob/master/drivers/mmc/core/sd.c)
are references for card-specific cache handling.

The September 27 verification passed a RISC-V/Sv39/SMP4 kernel build, focused
ext2 journal-rejection and MMC status-poll kernel tests, and the existing
16-cycle QEMU Firefox-state gate. The QEMU guest printed
`ASTERINAS_EXT2_FIREFOX_RECOVERY_OK cycles=16` after `sync` and unmount;
the host's read-only `e2fsck -fn` exited 0. Host-side board staging and
safe-reboot suites also passed. These are clean-shutdown and contract tests,
not a simulated SD power-loss or journal-replay test.

## September 27 follow-up: card preflight and the QEMU cut point

The board is currently in RockOS, with an authenticated root serial channel.
RockOS reports SD card `SR128` with SCR `0245848700000000`; the CMD48/49
support bit is set. That proves extension-command capability, **not** that
this particular card implements or has enabled a write cache. Before any
candidate boot, read-only `e2fsck -fn` on the unmounted Debian partition
(`RockOS /dev/mmcblk1p2`) returned 4: a Firefox profile `lock` directory
entry referred to a deleted inode, and inode bitmap counts disagreed. The
whole 4 GiB partition was copied to RockOS and then to the host; both copies
had SHA-256
`b7d20d3911a3f522bd6bf91c1effa859def01112c77bfccefcc574b80f34cdae`.
Checking that host image reproduced the same errors. Only then did one
offline `e2fsck -fy` repair the unmounted board partition (exit 1); a
subsequent `e2fsck -fn` returned 0. These are **pre-existing** structural
errors, not an outcome of the new cache code or an intentional power cut.
The full local evidence and immutable candidate hashes are under
`.local-test/sd-cache-20260927/`.

The first QEMU cut-point attempt revealed a test-harness mistake: the
RISC-V virtio device declaration order made `/dev/vda` refer to
`ltp_dev.img`, while the offline check inspected `ext2.img`. The test payload
was present and fsck-clean on `ltp_dev.img`, but this did not test the named
image. The device declaration order is now corrected, the guest scripts
require `/dev/vda /ext2 ext2` in `/proc/mounts`, and the recovery validator
uses `ASTERINAS_TEST_BUILD_DIR` when a cloned image is supplied. It also
checks an exact guest-written payload in that image, so a clean but wrong
image cannot pass. In a fresh
short run, QEMU was killed after the guest printed its post-`sync` marker.
The intended `ext2.img` contained the renamed payload with exact contents,
and offline `e2fsck -fn` returned 0. The 16-cycle Firefox-state gate was then
rerun with the corrected mapping on the same `ext2.img`; its `sync`, unmount,
transcript validator, and offline fsck all passed, and the cut-point payload
remained intact. This establishes the QEMU virtual-disk results, not physical
SD power-loss durability. The earlier 16-cycle result must be treated as a
test of `ltp_dev.img`, not of the intended `ext2.img`.

The next cut-point experiment omitted the global `sync`: it wrote and
`fsync`ed a new file, renamed it, `fsync`ed the parent directory, then killed
QEMU immediately after the guest marker. The final name and payload survived,
but `e2fsck -fn` returned 4 because the allocated inode and data block were
absent from their allocation bitmaps. A file-only `fsync` cut was also
inconsistent, but that is not conclusive by itself because it had not synced
the parent directory entry. The implementation fault was that inode-level
`fsync` flushed the inode table and block device while leaving ext2 allocation
bitmaps and group descriptors in memory. Inode `sync_all` and `sync_data` now
write those allocation records and the superblock before the device flush;
the group-descriptor dirty status is carried into the final metadata write so
that clearing the in-memory dirty bit does not suppress it. A fresh image at
the same directory-`fsync` cut retained the exact payload and passed offline
`e2fsck -fn` with exit 0.

A separate `blkdebug` test injected `EIO` specifically on virtual-disk
`flush_to_disk`. The guest's `fsync` returned `-1/EIO`, matching the injected
failure, rather than falsely reporting durable data. These two tests cover
the identified virtual ext2 writeback bug and flush error propagation. They
cannot establish physical SD persistence across an abrupt power loss.

The release kernel, Stage1, and DTB for a board probe were built, hash
checked, and staged on RockOS's separate partition without changing the
default boot entry. A read-only Basic boot first reached the kernel, but its
Stage1 lacked BusyBox. After packing the cached RISC-V static BusyBox into a
separate test archive, Basic reached its shell. In both runs the pre-armed
hardware watchdog reset the board, and the host restored RockOS and verified
root identity and boot ID after closing and reopening the serial port. This
proves a recovery path for this short diagnostic boot; the watchdog's window
is too short for a full desktop start.

The first writable candidate then booted with the existing kernel software
reboot deadline. The nonce-framed local root console survived serial reopen,
the ext2 root was verified as `/dev/mmcblk0p2`, and writing a unique value to
`/var/tmp/asterinas-sd-cache-probe` plus `sync` returned 0. The kernel logged
`[mmc] SD cache state: Enabled(...)`. A software reboot reached U-Boot;
RockOS was restored, its new root boot ID was verified across another serial
reopen, and offline `e2fsck -fn` of unmounted `/dev/mmcblk1p2` returned 0.
`debugfs` read back the exact written value. The preboot and postboot fsck
results were both 0. The local logs and artifact hashes are under
`.local-test/sd-cache-20260927/`.

The first writable candidate used an older installed safe-reboot script with
SHA-256 `2fb32a62ad5f4ad511c4a61a29eb6110ea9513d848aa58da46e77707a48f16e4`.
It called `sync` without first quiescing desktop writers. With RockOS in
control and the Debian partition unmounted, the script was backed up on the
separate RockOS partition and atomically replaced with the current source,
SHA-256 `ce81169cce45543ef43d633e6a176ae93d14da5cace366be35a1554a27498554`.
The source and installed hashes matched, and read-only offline `e2fsck -fn`
returned 0 before and after deployment. The second writable candidate then
booted the same release kernel, performed a unique persistent write and
explicit `sync`, and ran the new safe-reboot path. Serial output included
`ASTERINAS_USERSPACE_REBOOT_QUIESCE_DONE processes=0` and
`ASTERINAS_USERSPACE_REBOOT_SYNC_DONE`. After the software reboot, RockOS
returned with a different boot ID, its root serial channel passed a
close-and-reopen challenge, offline `e2fsck -fn` returned 0, and `debugfs`
read back the exact unique payload. The candidate result and serial log are
under `.local-test/sd-cache-20260927/`.

The two focused QEMU checks now have a one-command host gate:

```bash
python3 tools/riscv/ext2_durability_gate.py \
  --output-dir .local-test/ext2-durability-$(date +%Y%m%d-%H%M%S)
```

It creates a fresh ext2 image for each case, waits for the exact guest marker,
kills only that case's QEMU process after directory `fsync`, and checks both
offline metadata consistency and exact file contents. A separate fresh-image
run injects virtual-disk flush `EIO` and requires guest errno 5. The September
27 run passed both cases; the result JSON and full QEMU logs are under
`.local-test/sd-cache-20260927/durability-gate-run1/`. This is a short virtual
disk regression gate. No controlled physical power cut was performed or is
part of the current stability plan. The board results do not establish
SD-medium persistence under sudden power failure.

A normal graphical-target probe also completed a software-reboot cycle with
offline `e2fsck -fn` exit 0 and exact persistent payload readback. Xorg,
Openbox, the desktop background and panel were running, but the Firefox unit
restarted repeatedly and had no live browser process. The captured systemd
journal identified `ASTERINAS_FIREFOX_WEB_FAIL reason=invalid-network-profile`:
that probe's boot arguments omitted the explicit `ASTERINAS_WEB_NETWORK_MODE`
required by the browser profile. This is a test configuration error, not
evidence that the kernel failed to start Firefox. The browser launcher now
uses exit status 64 for an invalid profile, and its systemd unit suppresses
restarts for that permanent configuration error. A corrected one-boot probe
uses explicit `direct` mode and the basic-only browser profile, which does not
navigate public websites. The corrected board boot reached an active
`graphical.target` and Firefox unit; Firefox PID 144 was unchanged across a
12-second check and the unit reported `NRestarts=0`. Safe reboot printed both
quiesce and sync completion markers. RockOS returned, offline `e2fsck -fn`
exited 0, the exact persistent probe value was read back, and its root serial
channel passed a close-and-reopen challenge. The boot result is saved at
`.local-test/sd-cache-20260927/browser-boot.result.json`.

After this successful boot, the updated browser launcher and unit were copied
to the unmounted Debian partition from RockOS. The old files were hash-checked
and backed up on RockOS's separate partition first; temporary replacements
were hash-checked before atomic renames. Offline `e2fsck -fn` returned 0 on
both sides of deployment. A fresh read-only mount confirmed installed SHA-256
`e947056776dede86d53c6a7409062fd2469ab7dc8848249846f664b903670631`
for the launcher and
`422b26cd9e1a36fdf4f4c3221de93a9d1ed15c0158d84de219d68b72957a306b`
for the unit. The RockOS root serial channel remained available after another
close-and-reopen check. The deployed invalid-configuration exit behavior has
host-side tests and hash verification; it has not needed another board boot.

The current ext2 driver does not implement journal replay. It now rejects
`HAS_JOURNAL` as well as the already unsupported `RECOVER` incompatible bit.
Therefore `tune2fs -j` alone is
**not** a safe conversion for Asterinas: a nominally clean journaled volume
could be written without journal transactions. Reject journaled volumes until
the implementation actually supports their transaction and replay rules.

## September 27 follow-up: hard-link rename and repeated clean boots

A focused QEMU regression found a separate ext2/VFS namespace bug: renaming
one hard link over a second name for the same inode deleted the source name
and decremented the inode's link count. Commit `367f4f8c5` makes that operation
a no-op in ext2 and leaves both VFS dentries intact. Same-directory and
cross-directory user-space cases passed on a fresh RISC-V/Sv39/SMP4 ext2
image; a direct ext2 kernel test failed before the fix and passed afterward.
The guest synced and unmounted the image, and read-only `e2fsck -fn` exited 0.
This is a demonstrated semantic defect, not an established cause of the
earlier Firefox-profile corruption.

The corrected image also passed the existing 16-cycle Firefox-state workload.
After its first clean QEMU exit, offline `e2fsck -fn` returned 0 and `debugfs`
read the exact persistent marker. A second, separate QEMU launch reused the
same writable image, completed another 16 cycles, synced and unmounted, then
again passed offline fsck with the marker intact. Logs are under
`.local-test/sd-cache-20260927/rename-green/`, `rename-recovery/`, and
`rename-recovery-boot2/`. These were two clean virtual boots, not a physical
SD power cut or a new board validation of `367f4f8c5`.

The target desktop path is: a read-only, recoverable base system image plus a
persistent **journaled** filesystem for `/home` and writable system state.
The smallest standards-compatible implementation path is ext3-style metadata
journaling on the existing ext2 layout: group each directory entry, inode,
bitmap and block-pointer update into one transaction; write file data before
committing related metadata; flush the commit record to proven-stable media;
replay committed transactions before mounting read-write; and recover orphaned
unlinked inodes. This reuses the current inode/block format but is still a
substantial kernel feature, not a mount option. Linux's
[ext2 journaling explanation](https://docs.kernel.org/6.1/filesystems/ext2.html)
and [ext4 journal contract](https://docs.kernel.org/filesystems/ext4/journal.html)
describe the required crash boundary. Unsynced latest application data may
still be lost; the guarantee sought here is a mountable, structurally
consistent filesystem and correct `fsync` behavior.

## Temporary experiment containment, not a daily-use policy

1. During risky development boots only, check the **unmounted** partition
   from RockOS (`/dev/mmcblk1p2`, UUID
   `c2ce5134-afcc-4d7c-b71e-7e6d4a8f2b10`) with `e2fsck -fn` and require
   exit 0. Asterinas names this partition `/dev/mmcblk0p2`. Keep the raw
   September 15 and September 26 images until the fault is classified.
2. If fsck fails, stop the selected boot. Capture its full output and a
   byte-verified partition image before considering an offline repair. Never
   run a modifying fsck while mounted, and do not make `e2fsck -y` an
   automatic boot step.
3. Use the bounded userspace shutdown path and verify its `SYNC_DONE` marker
   before relying on a clean reboot. Preserve the kernel watchdog and the
   RockOS default as recovery paths. Offline fsck remains a test oracle and
   emergency repair tool; it must not become a step the desktop user performs
   on every boot.
4. The September 27 host-side staging helpers now enforce the writable-boot
   recovery guard even when called outside their CLI parsers. A missing kernel
   timer is rejected; an explicit userspace deadline must precede it with
   headroom. The safe-reboot script now reads the kernel timer even when an
   explicit userspace deadline is supplied, preserving bounded quiesce and
   sync operations. The service can also derive its userspace deadline from
   the kernel timer when no override is present. These fixed guard gaps are
   not proven causes of the previous corruption.

## Short diagnostic sequence

1. Clone one clean ext2 QEMU image for each run. Use the Firefox profile's
   actual create/replace/rename/unlink/SQLite-shm pattern, not a long load.
   The existing 16-cycle recovery gate does `sync` and unmount, then checks
   fsck; retain it as the clean-shutdown baseline.
2. Add controlled cut points after directory entry mutation, inode link-count
   writeback, block release/reallocation, and sync completion. For each cut,
   run offline `e2fsck -fn` and save the first failing operation and affected
   inode/block IDs. Run the same operations on Linux ext2 as a reference.
3. If a clean `sync`/unmount run fails, trace Asterinas writeback and
   allocation ordering first. If failures occur only at power cuts, compare
   their type and frequency with Linux ext2 before labeling them an
   Asterinas-specific bug. Turn a reproducible difference into a focused
   regression test and fix one cause at a time.

For experimental GPU boots, a read-only base plus tmpfs/overlay upper layer
can keep Firefox's write-heavy profile away from the persistent ext2 image,
at the cost of losing that profile on reboot. This is a containment measure,
not a durable desktop solution. The current Asterinas filesystem registry
includes ext2 and overlayfs but no ext4 implementation. Simply reformatting
this partition as ext4 would make it unmountable by today's kernel.

## Acceptance without operator fsck

- In QEMU, use cloned images and deterministic resets at metadata and journal
  commit points. The automated test may use offline `e2fsck` to validate the
  result; a normal Asterinas boot must perform journal replay itself.
- After each reset, require a mountable filesystem, no dangling directory
  entries or multiply-claimed blocks, and no `ESTALE` in a fresh Firefox
  profile. Check `fsync` and parent-directory durability separately.
- On the board, verify SD write completion and flush behavior with card
  capability/status evidence and a readback after a real reboot. Then run one
  short Firefox workload, reboot at controlled points, and require recovery
  without RockOS repair. Retain RockOS as the recovery entry while this gate
  is developed.

## Follow-up: propagate ext2 block-reclaim failures

The inode block-pointer truncation path previously logged and ignored errors
from indirect-block reads and block release. Deleted-inode reclaim could then
free the inode bitmap even though blocks were still allocated or referenced.
The path now reports failure, clears each pointer only after its subtree has
been freed, and leaves the inode allocated if reclaim fails. A failed regular
file shrink also reports the error to its caller. Directory removal now deletes
the parent entry before dropping the child link count, matching unlink's error
ordering.

Focused RISC-V/Sv39 kernel tests cover an injected indirect-block read error
followed by a successful retry, direct and multi-level truncation, and unlink
immediately after a write. The normal RISC-V kernel build also passes. These
checks do not establish crash atomicity: ext2 still has no journal or orphan
replay, and the separate intermittent QEMU second-boot failure remains under
investigation. No controlled power-cut experiment was run for this follow-up.
