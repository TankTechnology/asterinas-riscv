# Browser root filesystem recovery during GPU bring-up

The first selected Asterinas restoration entered the kernel and brought up
the 1920×1080 DRM desktop, but `asterinas-browser-web.service` restarted
repeatedly. Firefox logged `ESTALE` while writing its profile database.
RockOS then mounted the Asterinas `ASTER_BROWSERWEB` ext2 partition read-only
and reported that the `user.js` directory entry referenced a deleted inode.
The [failed boot result](pre-repair-boot.json) records the automatic recovery.

After unmounting the partition, `e2fsck -fn` found a deleted-inode reference
for `user.js`, an invalid HTree index in Firefox `cache2/entries`, unattached
inodes, and bitmap discrepancies. The 4 GiB partition was copied **before**
repair to the RockOS filesystem at
`/home/debian/asterinas/p2-browserweb-pre-fsck-20260926.img`; both the copy
and source had SHA-256
`bc46e7213421351f9bfe9ae98a2f0c4421406f3a091964d347163da9e76051f2`.
The image is retained outside Git. A second copy was repaired first and passed
another read-only `e2fsck`; only then was the live partition repaired. The
repair cleared the stale `user.js` entry, which Firefox's startup script
regenerated on the next boot. A fresh read-only `e2fsck` on the live partition
reported no errors before that boot.

The ext2 driver used linear directory updates but left the HTree `INDEX_DIR`
flag on mutated parent directories. Commit `b063c147f` clears that flag on
create, link, unlink, rmdir, and rename, including a moved directory's `..`
update. A targeted RISC-V QEMU kernel test failed on the old behavior and
passed after the change (`1 passed; 0 failed`). The new RISC-V Image SHA-256
was `61aacb07f75b0aebec915e07cbec8accf2dd6e8a36bd34c8c52f69ccee49561d`.

The [selected post-repair boot](post-repair-boot.json) reached Asterinas boot
ID `a4162627-e004-4649-81df-fa475320a4eb`. Fresh serial root UID 0 and
boot identity were verified after closing and reopening the host serial port;
the watchdog was disarmed. Xorg reported 1920×1080, `/dev/dri/card0` was
present, and Firefox PID 92 stayed `running` with `NRestarts=0` in a second
check 15 seconds later. The recreated `user.js` was 1495 bytes and the new
Firefox stderr log had zero `Stale file handle` occurrences at that check.

This proves a bounded desktop restoration, not long-term filesystem
correctness. The previous corruption may have more than one cause: inode
reclaim/writeback ordering and interruption during earlier experimental boots
still need isolated tests. Keep the raw image until post-reboot filesystem
checks and continued daily use pass. The persistent U-Boot default remains
RockOS; the selected Asterinas boot currently has a verified serial root
control path.
