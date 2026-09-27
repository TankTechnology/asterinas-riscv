# exFAT sync and directory-entry validation, September 27, 2026

This follow-up covers QEMU filesystem correctness. It does not test sudden
power loss, physical SD persistence, or the Megrez desktop boot. The board's
daily-use root filesystem remains ext2; these exFAT failures are separate
bugs found while auditing the writeback interfaces.

## Failures reproduced before the fix

On fresh 64 MiB `mkfs.exfat` images, a one-shot QEMU `blkdebug` failure on
`flush_to_disk` made both exFAT `fsync` and `syncfs` return success even though
the block layer recorded `BioStatus::IoError`. The `fsync` and `syncfs`
implementations either discarded the returned BIO status or skipped the final
device flush. Logs and scratch images are in
`target/exfat-fsync-red-20260927/` and
`target/exfat-syncfs-red-20260927/`.

A clean exFAT write/unmount then produced directory entries that
`fsck.exfat -n` rejected with incorrect name hashes. An ASCII and a Unicode
filename both reproduced this on the old kernel in
`target/exfat-name-red-20260927/`. The name hash had been computed from the
original UTF-16 name, while exFAT requires the up-cased UTF-16 name. The
driver also loaded only the first 128 up-case mappings and performed some
lookups with case-sensitive comparison. The required name-hash and up-case
table rules are in the [Microsoft exFAT specification](https://learn.microsoft.com/en-us/windows/win32/fileio/exfat-specification),
sections 7.2.5 and 7.6.4.

## Fix and bounded validation

The exFAT implementation now checks the device flush status for `fsync`,
`fdatasync`, and filesystem sync. It decodes and validates the full on-disk
up-case table, uses that table to compute directory-entry name hashes, and
uses case-insensitive lookup for ordinary path operations. The on-disk name
still retains its original spelling.

On the fixed kernel, fresh one-shot `blkdebug` cases returned `EIO` for both
syscalls and exited normally. Evidence is in
`target/exfat-fsync-final-20260927/` and
`target/exfat-syncfs-final-20260927/`. A clean first QEMU boot wrote ASCII
and Unicode filenames and unmounted exFAT. A second independent QEMU boot
read each through its original and uppercase spelling, renamed and deleted
through uppercase aliases, then unmounted it. Offline `fsck.exfat -n`
reported the image clean after each boot; the image and logs are in
`target/exfat-name-green-20260927/`.

The normal ext2 software-reboot gate also passed on this kernel build: two
QEMU boots, persisted payload readback, and read-only `e2fsck -fn`. Its
evidence is in `target/reboot-sync-exfat-final-20260927/`. This is a startup
regression check, not an exFAT power-loss claim.

## Limits and next stability work

This does not establish exFAT crash consistency. The up-case table loader
still assumes its on-disk cluster chain is physically contiguous; fragmented
tables need a separate fix and test. For the desktop root, continue the
bounded ext2 startup and clean-reboot gates while designing a journaled
writable layer. Keep offline filesystem checks as test oracles, rather than
turning them into a routine user boot step.
