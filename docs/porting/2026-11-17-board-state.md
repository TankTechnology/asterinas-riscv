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
* p2 (the Debian root) **cannot be mounted**: the kernel returns `ENODEV` for
  `mount -t ext4` (and for `-t ext2`), with both the rebase kernel and the
  deployed release kernel, so this is the filesystem, not the kernel. Raw
  block reads work (200 MB in 10 s); U-Boot can still *list* directories and
  read `/root/kernel-rel.img`, but `ext4load` of `/usr/sbin/e2fsck` and of the
  library files fails, so the inode/data damage is real.

## How it got there

p2 was fsck-clean at ~15:30 (`FSCK_RC=0`, `VERIFY_RC=0` after two `-fy`
passes). It was 92 % full. Several boots then ran the desktop with systemd
hanging after `exec`; the kernel kept writing to p2 (journald) while systemd
was being killed and restarted, and after those boots the superblock could no
longer be read. Treat "reboot into a systemd that hangs" as a write hazard on
this rootfs.

## Recovery path for the next session

There is no e2fsck on the board that can run today:

* The repair initramfs (`mmc 1:3 repair.cpio.gz`) copies `e2fsck` out of p2
  after mounting it read-only -- and that mount is exactly what fails now.
* U-Boot cannot read the `e2fsck` binary off p2 (`Failed to load`).
* RockOS (menu entry 1) drops to an `(initramfs)` shell on this board and has
  no e2fsck.
* The host has no riscv64 e2fsck or e2fsprogs package.

So the practical options are, in order:

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
