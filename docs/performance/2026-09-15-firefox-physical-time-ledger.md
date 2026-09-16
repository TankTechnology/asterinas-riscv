# Firefox physical time ledger, 2026-09-15

This is a partial measurement, not a Firefox performance result. The current
kernel artifact was SHA-256 `0c2da50816abea7d6be6aefc092acccd31b64f2421aceeed8047f2a5b97f956b`;
the deployed Stage1 was SHA-256
`9ed54c5a6f882ed37a066f88f040051a4f81326e9a2d8e961dfbceead44a1ef7`.
Both boots used the same DTB, CRC32 `40ed4c65`, and a 900-second emergency
recovery timer. Neither boot modified the U-Boot menu or provisioned partition
2 from RockOS. The local proxy and fixture were held at `10.100.19.216:17893`
and `:17894`.

| Phase | Direct evidence | Interpretation |
|---|---|---|
| First boot, isolated root console | Kernel and root shell entered; `systemctl get-default` was `asterinas-debug-console.target`; all three graphical probes were inactive. | This mode intentionally does not auto-start desktop. It cannot provide Firefox startup measurements without an explicit guest command. |
| First boot, guest control | The serial shell received only a prefix of `/run/asterinas-tools/physical-graphics-control start-browser` and reported that the truncated path did not exist. | No browser-launch failure can be inferred from a command that did not arrive intact. |
| Second boot, normal root console | Kernel, Debian root, network and desktop console probes passed. The physical network script reported 20 local fixture transfers (1,310,720 bytes), HTTPS 200 from `www.baidu.com`, and the Baidu logo asset. | Network readiness passed before the browser workload. These are connectivity checks, not page-render timings. |
| Second boot, graphics | `pgrep -x Xorg` reported PID 102; Firefox process queries reported no PID. `asterinas-browser-web.service` was still `activating` when queried. | A browser-window, keyboard, pointer, public-page, or local navigation latency cannot yet be reported. The next target is the service dependency/wait-X timeline. |
| Recovery | Both boots returned to RockOS under the kernel timer. SSH to RockOS recovered. | The safety fallback worked, but it is an emergency restart, not a synced shutdown. |

The Stage1 tools for `/proc/stat` + Firefox/Xorg CPU windows and bounded local
`requestAnimationFrame`/NavigationTiming capture were unit-tested and deployed,
but not run on a live Firefox PID. The root console did inherit the exact local
fixture URL environment. No measured kernel hot path has yet been optimized.

## Filesystem safety finding

After the second boot, RockOS showed `/dev/mmcblk1p2` (ext2) unmounted. The
read-only `e2fsck -fn /dev/mmcblk1p2` exited 4, finding four `man-db.service`
private directories in `/var/tmp` whose entries referred to freed inodes,
another stale `openbox.log` directory entry, wrong link/bitmap counts, and a
directory count mismatch. A separate read-only mount reproduced `Structure
needs cleaning` for the `/var/tmp` entries. `/var/tmp` had mtime
`2026-09-15 17:38:47 +0800`; the `openbox` directory had mtime
`2026-09-11 22:22:21 +0800`. These directory timestamps do not date the
dangling entries themselves. There was no pre-boot fsck in this experiment, so
we cannot attribute each inconsistency to a specific boot. The RockOS clock was
behind the host, which explains e2fsck's independent "superblock write time in
the future" warning, not the structural errors.

`kernel/src/boot_reboot.rs` invokes `power::emergency_restart` at the 900-second
deadline. The optional `megrez-safe-reboot` service in the Debian image instead
calls `sync` before `reboot -f`, but these two boot arguments did not set
`ASTERINAS_SAFE_REBOOT_AFTER`. An emergency restart can cut across ext2
metadata writes; this is a plausible mechanism for the new dangling entries,
not a proof that it caused every reported inconsistency. The read-only mount
was unmounted and its temporary mountpoint removed; partition 2 remained
unmounted in RockOS. At the time of this initial finding, no filesystem repair
had been performed.

Before another writable desktop boot, repair/backup policy needed an explicit
decision. The physical launcher now rejects a writable partition plus kernel
emergency deadline unless the boot arguments also specify a userspace synced
reboot at least 120 seconds earlier. The fallback remains for genuine hangs;
it cannot promise crash-consistent ext2 metadata if a hang defeats userspace
sync. A disposable QEMU ext2 image is the appropriate place to check rmdir,
unlink, and sudden-reboot behavior without risking the board data.

## Backup and authorized repair

The user approved a local RockOS backup and repair before further physical
sampling. `/dev/mmcblk1p2` was still unmounted and exactly 4,294,967,296 bytes.
RockOS copied all of it to `/home/debian/asterinas-p2-backup-20260915.img` with
`dd` and `conv=fsync`; `cmp -n 4294967296` established a byte-for-byte match
before repair. The backup is mode 0600 and its SHA-256 is
`b58a44ba6f63b148dc36272886da5f3ca1458431567cd547d6d26f9ce235a610`.
The backup must not be overwritten or deleted by subsequent experiments.

The RockOS clock was corrected to host UTC before repeating the read-only
check; the structural failures remained, but the false future-superblock
warning disappeared. `e2fsck -fy /dev/mmcblk1p2` was run with the partition
unmounted. Its complete repair transcript and exit status were lost when the
tool session was interrupted, so the exact repair actions cannot be asserted.
A fresh post-repair `e2fsck -fn /dev/mmcblk1p2` completed all five passes and
exited 0, reporting 19,504/131,072 files and 231,204/524,288 blocks. This
establishes current filesystem consistency, not the cause of the original
damage or the behavior of the next boot.

## One safe-deadline physical follow-up

A third boot used the same kernel, DTB, and deployed Stage1 hashes. It removed
`--volatile-home` so browser files could survive a normal synced reboot, and
set `systemd.setenv=ASTERINAS_SAFE_REBOOT_AFTER=780` before the kernel's
`asterinas.reboot_after=900`. The physical launcher validated this ordering,
and U-Boot loaded all three pinned files with the expected CRC32 values. The
local fixture and proxy each returned HTTP 200 when tested from RockOS before
reboot. The serial launcher measured 5.94 seconds from kernel-enter to the
systemd userspace milestone; graphical and desktop state probes were active,
and the 20-transfer fixture and Baidu HTTPS checks passed.

After desktop startup, a single `systemctl show` query on the root UART did not
return a prompt within 25 seconds. A subsequent Ctrl-C also elicited no UART
output. ARP and RockOS SSH to the board address did not respond. This is an
observed guest communication stall, not proof that Firefox caused it or that
all CPUs stopped. Passive serial capture remained silent until a fresh
OpenSBI/U-Boot firmware epoch appeared after the 900-second kernel fallback.
No userspace synced-reboot marker was observed, and RockOS was not reachable
at the 780-second userspace deadline. The actual cause of the missing
userspace reboot remains unknown.

The deployed partition-2 `/usr/lib/asterinas/megrez-safe-reboot` is a 5,095-byte
older quiesce-first script, not the current 1,406-byte simplified source at
`tools/riscv/debian/rootfs/megrez_safe_reboot.sh`. It bounds service stop at
60 seconds, forced unit kill at 15, user-process TERM wait at 30, KILL wait at
15, and `sync` at 45, before reboot. Their sequential budget is already 165
seconds, greater than the launcher's 120-second minimum gap; overhead and
`timeout --kill-after` allowance increase it further. The old script computes
its own 180-second guard only when no explicit userspace deadline is set.
Thus the launcher accepted a deadline which is insufficient for the deployed
script's worst-case cleanup. This is a verified configuration mismatch, not
evidence that its quiesce path was actually reached in the stalled boot.

RockOS again found `/dev/mmcblk1p2` unmounted. After correcting its clock, the
post-boot read-only `e2fsck -fn` exited 4. It reported one new stale
`man-db.service` private directory entry in `/var/tmp` inode 19127, referring
to freed inode 17207; unattached inode 17210; inode 19127 link count 3 rather
than 2; block bitmap differences at 11149 and 11160; inode bitmap differences
at 17207--17208 and 17210; and group-2 directory count 291 rather than 289.
`debugfs` showed inode 17207 as a zero-link directory with deletion time
2026-09-15 18:58:46 +0800; inode 17210 was a zero-length regular file owned
by UID 1000 with the same creation-time second. These facts establish that
the filesystem became inconsistent during this boot, but cannot distinguish
an ext2 ordering defect from the expected crash window of a non-journaled
filesystem when the emergency timer bypasses sync.
The [Linux kernel ext2 documentation](https://docs.kernel.org/filesystems/ext2.html)
explains why a non-clean ext2 shutdown needs `e2fsck` and why journaling
protects metadata consistency across an incomplete transaction; it does not
diagnose this Asterinas-specific failure.

The per-user Firefox timeline, stderr, and PID files on partition 2 still had
their 2026-09-11 timestamps. Their unchanged on-disk state does not prove the
services never ran: emergency restart may have discarded unflushed writes.
No live Firefox PID, local navigation, rAF, keyboard, pointer, or CPU timing
sample was captured. A read-only `e2image -Q` metadata snapshot of the new
failure was saved at
`/home/debian/asterinas-p2-posthang-20260915.qcow2` (25,325,568 bytes, mode
0600). The earlier byte-exact 4-GiB backup is unchanged. Further writable
physical boots and automatic repairs are paused pending a smaller safe
experiment and a crash-independent observation channel.
