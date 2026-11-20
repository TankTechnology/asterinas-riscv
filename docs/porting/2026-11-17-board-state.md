# Megrez board state after the dirty-bit work (2026-11-17, end of session)

The kernel side of this session is finished and verified (see
`2026-11-17-dirty-bit-fault-loop.md`): the EIC7700 dirty-bit fault loop is
fixed, the deterministic probe passes, the fixed kernel booted the full
desktop in 13--14 s with both evdev devices, framebuffer, HTTPS and the eBPF
tool suite.

The **board's root filesystem is now unmountable**, and that is what the next
session has to deal with first.

## What the board is in now

* U-Boot works. `bootcmd` (saved in SPI flash) was changed to load the kernel
  from the data partition, because p2 reads had become too slow for the
  watchdog window:

  ```
  ext4load mmc 1:3 0x80200000 kernel-rel.img; ext4load mmc 1:1 0x88300000 stage1-daily.cpio;
  setenv initrd_size ${filesize}; ext4load mmc 1:1 0x88000000 megrez-daily.dtb;
  setenv bootargs ${as_args}; booti 0x80200000 0x88300000:${initrd_size} 0x88000000
  ```

  with `as_args = console=ttyS0 loglevel=warn init=/init
  asterinas.mmc_write_partition2 asterinas.mmc_write_partition3
  asterinas.reboot_after=900 asterinas.hardware_watchdog_handoff=1
  asterinas.panic_reboot=1 asterinas.net=eic7700-rj45,58.206.232.122/255.255.255.0,58.206.232.1
  -- --root-init=systemd --debug-console=isolated-root`.
  The documented boot.scr path can be restored with
  `setenv bootcmd 'ext4load mmc 1:2 0x87000000 /root/boot.scr && source 0x87000000'; saveenv`.
* p3 (`kernel-rel.img`, `repair.cpio.gz`) reads at 58 MiB/s and is healthy.
* p1 (initrd + dtb) is healthy.
* p2 (the Debian root) **cannot be mounted**, by three different kernels: the
  rebase kernel, the deployed release kernel, and `main 7564b2976` from the
  p1 extlinux menu. Each reaches `probe-block … result=match` →
  `root-found device=/dev/mmcblk0p2` and then fails `handoff-failed
  action=root-mount` in the same millisecond, while the vendor kernel mounts
  the same filesystem read-write. Raw block reads work (200 MB in 10 s);
  U-Boot can still *list* directories and read `/root/kernel-rel.img`, but
  `ext4load` of `/usr/sbin/e2fsck` and of the library files fails, so there is
  real inode/data damage as well.
* **Do not read that `ENODEV` as "the filesystem is bad".** The two mount paths
  mean different things by it: a user `mount(2)` returns `ENODEV` only when the
  block device cannot be resolved (`fs/vfs/fs_apis/registry.rs:204`, `:210`),
  and it passes the ext2 driver's own `EINVAL` checks through unchanged
  (`fs/fs_impls/ext2/super_block.rs`); the kernel's root mount instead maps
  every `EINVAL` from every candidate filesystem type to
  `ENODEV` (`fs/rootfs.rs:117`). Which of the two happened is **not yet
  established**; the leading suspects are one of the ext2 superblock checks and
  a devtmpfs-node/`aster_block`-registry mismatch (two node creators live in
  `device/model.rs` and `device/registry/block.rs`). Decide it with
  `loglevel=debug` and a `mount` run from a live system, not by inference.
  `/tmp/board-net/ext2check.py` replays every ext2 check on an image offline;
  the newly built image (`5b6fc41e…`) passes all of them.

## How it got there

p2 was fsck-clean at ~15:30 (`FSCK_RC=0`, `VERIFY_RC=0` after two `-fy`
passes). It was 92 % full. Several boots then ran the desktop with systemd
hanging after `exec`; the kernel kept writing to p2 (journald) while systemd
was being killed and restarted, and after those boots the superblock could no
longer be read. Treat "reboot into a systemd that hangs" as a write hazard on
this rootfs.

## Recovery path

A ready-to-write replacement now exists: the Debian root was rebuilt from
`tools/riscv/debian/rootfs/build_rootfs.sh` (profile `browser-web`,
trixie/riscv64, 4 KiB-block ext2) and the finished image sits on the board
itself at p3 `/root/img` — 3,221,225,472 bytes, sha256
`5b6fc41e9b2bcdc1599f8bc4ec1e9cf171d54440b2d078c0ea2fd03b96d9ee25`, identical
to `target/debian-riscv/browser-web/rootfs/debian-root.ext2` on the host.

There is still no e2fsck on the board that can run:

* The repair initramfs (`mmc 1:3 repair.cpio.gz`) copies `e2fsck` out of p2
  after mounting it read-only -- and that mount is exactly what fails now.
* U-Boot cannot read the `e2fsck` binary off p2 (`Failed to load`).
* RockOS (menu entry 1) drops to an `(initramfs)` shell on this board and has
  no e2fsck.
* The host has no riscv64 e2fsck or e2fsprogs package.

So the plan is to write the replacement image, not to repair the old one:
boot a state where p2 is **not** mounted (RockOS entry 1 lands in an
`(initramfs)` shell on this board), mount p3 read-only, keep a 1 KiB copy of
the old superblock for the diagnosis above, then
`dd if=/root/img of=/dev/mmcblk1p2 bs=1M` (the vendor kernel names the card
`mmcblk1`), `sync`, reboot. Judge success by `handoff-done action=root-mount`
and `ASTERINAS_DESKTOP_BOOT_READY`.

The alternatives, if that route is unavailable:
1. Build a maintenance initramfs that carries a riscv64 `e2fsck` + its
   libraries itself (a Debian riscv64 `e2fsprogs` package provides them), then
   get it onto p1 or p3 and boot it. U-Boot has no tftp/wget, so that means
   `loady` (YMODEM) over the serial port or writing it from a running
   system -- both need a working rootfs or a working YMODEM.
2. Recreate the Debian rootfs on p2 from its builder and re-deploy
   `kernel-rel.img`/`boot.scr` (the documented product layout). This is the
   clean path given how much the filesystem has been through today.
3. Only if the data is needed: mount nothing, and use `debugfs` from another
   machine on a raw image of p2.

## Outcome (2026-11-18): the root filesystem was never the problem

Booting the repair image with `rdinit=/bin/sh` gave a shell on the same kernel and
the same card, and every mount succeeded -- `mount -t ext2 -o ro /dev/mmcblk0p3`
`RC=0`, `mount -t ext2 -o ro /dev/mmcblk0p2` `RC=0`, and `mount -t ext2
/dev/mmcblk0p2` **read-write** `RC=0`. A reboot with the identical boot
configuration (kernel from p3, stage1 and DTB from p1) then walked the whole
handoff to `handoff-done` and reached `ASTERINAS_DESKTOP_BOOT_READY` twice
(12.8 s and 187 s), twice printing `USERSPACE_REBOOT_DISARMED reason=desktop-ready`.

So the replacement image on p3 was **not** written: p2 is healthy. The old
superblock region is kept at p3 `/p2-head.bin` (64 KiB) in case the original
`handoff-failed` episode needs explaining, and `/img` stays on p3 as a spare copy.

`handoff-failed action=root-mount` only says that one `mount()` call from stage1
failed. It does not mean the filesystem is damaged: mount it by hand from the same
kernel before concluding anything.

## Open: an early machine-level reset that is not the watchdog

Hand-driven boots then started resetting the board about 1.4 s into the kernel
(right after the USB ports come ready), and the reset is a full power-on reset --
DDR self test and HDMI PHY training run again. Ruled out so far:

* the DesignWare watchdog: with the gate enabled (`mw.l 0x51828444 0x1`)
  `0x5080000c` reads 0 (disabled), the board idles at the U-Boot prompt for 90 s
  without a reset, and the persisted `bootcmd` arms no watchdog;
* the kernel image: both `kernel-rel.img` (9,031,192 B, p3 and p2) and the older
  `kernel-rel.img.bak` (9,685,752 B, p2) reset;
* the filesystem: see above.

The same kernel had booted to the desktop twice minutes earlier through `bootcmd`,
so the reset is state-dependent. Not yet tested: an explicit
`asterinas.panic_reboot=false` boot that verifiably reached `booti` (the attempt
was lost to a dropped console line), which would tell a panic reboot from a
SoC-level reset.

## The plan that was prepared before the outcome above (kept for the next time)


Do not use the vendor kernel or the extlinux menu for this. The repair image
on p3 already carries a **static busybox** (`mount umount cat echo ls` are
symlinks to it, but `busybox dd`, `busybox sha256sum`, `busybox mkdir` all
work), and its own init runs `e2fsck -f -y` on p2 and p3 before handing over
a shell. Booting it with `rdinit=/bin/sh` skips that fsck, which matters
because the fsck rewrites the p2 superblock and destroys the evidence of why
Asterinas refused the filesystem.

```
ext4load mmc 1:3 0x80200000 kernel-rel.img
ext4load mmc 1:3 0x88300000 repair.cpio.gz
setenv initrd_size <the size printed just above; do not trust a stale ${filesize}>
ext4load mmc 1:1 0x88000000 megrez-daily.dtb
setenv bootargs console=ttyS0 loglevel=warn rdinit=/bin/sh asterinas.mmc_write_partition2 asterinas.mmc_write_partition3 asterinas.reboot_after=1800
booti 0x80200000 0x88300000:${initrd_size} 0x88000000
```

`rdinit=` selects the initramfs init (`fs/initramfs.rs:308`); `init=` only
applies to the filesystem that is mounted as root, so `init=/bin/sh` would
not have skipped the repair script.

In that shell, decide the failure class **before** writing anything:

* `mount -t ext2 -o ro /dev/mmcblk0p3 /mnt3` succeeds while p2 fails
  -> the p2 filesystem is the problem, and writing the image fixes it.
* neither mounts -> the device registration path is the problem
  (`aster_block::lookup`), the image will not help, and the kernel has to be
  fixed first.

Then keep `dd if=/dev/mmcblk0p2 of=/mnt3/root/p2-head.bin bs=1024 count=64`
(the old superblock and the start of the group descriptors), and only after
that `dd if=/mnt3/root/img of=/dev/mmcblk0p2 bs=1M`, `sync`, and
`echo b > /proc/sysrq-trigger`.

## Never use the raw mmc commands in this U-Boot

`mmc dev 1` on this board is flaky (`Card did not respond to voltage
select! : -110`) and `mmc read 0x80000000 0xfa022 0x4` **crashes U-Boot**:

```
sbi_trap_error: hart0: trap0: s6=0x00000fa022 s7=0x00000001   (block, count)
```

OpenSBI then parks the hart: no output, no watchdog (U-Boot does not arm one),
so the board needs a **power cycle**. `ext4ls`/`ext4load`/`ext4write` on the
same card are fine, which is why every load above uses them.

Also on this board: the extlinux menu silently ignores typed choices here
(twice: `Enter choice: 9:` with no echo of a `1`), so a menu timeout picks its
default entry and its baked-in `asterinas.reboot_after=600`. Hand-drive
`booti` instead of relying on the menu.


## Board facts worth keeping

* The DW watchdog keeps running across resets once armed. A boot that does not
  pass `asterinas.hardware_watchdog_handoff=1` (the repair initramfs, for
  example) is reset ~10 s in, mid-fsck. Any hand-driven boot of the repair
  image must include that parameter, or disarm the watchdog
  (`mw.l 0x5080000c 0x0`) first.
* The repair initramfs mounts p2 in `/init` and unmounts it again after
  copying the tools, so its shell has an empty `/mnt`; mount p2 yourself.
* `/tmp` does not exist in the repair initramfs until you `mkdir` it.
* Kernel-image placement matters on this card: the same 9 MB image reads at
  58 MiB/s from p3, 11 MiB/s from a freshly scp'd file on p2, and 2 MiB/s from
  a file written by `cp`/`ext4write` on p2. A 4 s kernel load plus `dhcp` is
  enough for the watchdog to reset a boot that would otherwise succeed.
