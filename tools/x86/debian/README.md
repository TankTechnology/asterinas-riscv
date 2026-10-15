# Debian on the x86_64 kernel

Boots a Debian root filesystem on the x86_64 Asterinas kernel, and runs the
pinned native LMBench suite in it.

The rootfs itself is built by the architecture-neutral tooling; see
[`tools/debian/README.md`](../../debian/README.md). What lives here is the boot,
which differs completely from RISC-V's.

## `boot_debian.sh`

```sh
tools/x86/debian/boot_debian.sh \
  --kernel target/osdk/asterinas-osdk-bin.elf \
  --initramfs target/debian-x86/stage1/initramfs.cpio \
  --root-image target/debian-x86/m6/rootfs/debian-root.ext2 \
  --output target/x86-debian/boot.transcript \
  --wait-for "Reached target multi-user.target"
```

QEMU loads the Asterinas ELF itself: `-kernel` takes the multiboot2 image and
`-initrd` becomes its first module, which the kernel unpacks as a cpio. No GRUB
and no firmware. OSDK produces the ELF for this with
`--boot-method vmm-direct` — that path patches the image's `e_machine` to
`EM_386`, which QEMU requires and GRUB does not.

Stage1 finds the Debian disk by its ext2/ext4 volume label, so the disk may sit
on any virtio-blk slot; the script attaches exactly one, which lands on
`/dev/vda`.

`--print-argv` and `--print-append` print the QEMU command line and the kernel
append string without running anything, which is what the tests assert.

### Device flags that are not optional

- Every virtio device carries `disable-legacy=on,disable-modern=off`. With
  QEMU's default transitional `virtio-blk-pci` the guest gets no block device
  at all, and Stage1 reports `root-discovery-timeout` — it only announces
  `probe-block` for paths that exist *and* are block devices, so an
  unrecognised disk looks exactly like an absent one.
- No `-nographic`. It hands stdio to the serial port implicitly, which
  collides with the mux chardev the guest console needs, and QEMU refuses to
  start with `cannot use stdio by multiple character devices`.
- `console=hvc0` in the append string. There is no 16550 driver, so
  `console=ttyS0` silently falls back to the VGA console, which `-display none`
  discards.
- `loglevel=4`. Above it the kernel prints a line per syscall and `ldconfig`
  alone runs past 100 seconds of guest time.

The append string places `--root-init=systemd --root-fs=ext4` after a literal
`--`, which is the only way the kernel turns them into init argv. It is passed
to QEMU directly rather than through the OSDK scheme because `[run.boot]` in
`OSDK.toml` always appends `sh -l` to the init arguments and no CLI flag or
scheme removes them; Stage1 rejects the unknown token.

## `prepare_lmbench_disk.sh`

```sh
tools/x86/debian/prepare_lmbench_disk.sh \
  --base-root target/debian-x86/m6/rootfs/debian-root.ext2 \
  --runtime target/lmbench-native-x86/runtime.tar.gz \
  --output target/x86-debian/lmbench-root.ext2
```

Copies a published rootfs and installs the LMbench runtime, the workload unit
and the wildcard-bind regression binary into it, so the guest only runs the
benchmark. Needs root: the image is loop-mounted.

The runtime archive comes from
`python3 tools/riscv/lmbench_native.py package --architecture amd64 --output …`,
run inside the development container because it uses Nix.

The disk also carries
`/etc/systemd/system/systemd-journald.service.d/asterinas-notify-compat.conf`.
This kernel cannot complete systemd's `Type=notify` readiness handshake, so
without it the journal service times out and every unit ordered after the
journal socket waits for that start job.

## What the run produces

The guest reaches the upstream driver's own `make results` and reports a
coverage verdict rather than a pass/fail on make's exit status. On this kernel
the suite runs to completion and measures 105-106 of 109 groups:

```
LMBENCH_RUN_NATIVE_EXIT 2 …      make exit 2: the coverage gate, not a crash
  "native_exit_status": 0,       the inner make succeeded
  "elapsed_seconds": 2364.1,
  observed: 106,  missing: ['signal-prot','rpc-udp','rpc-tcp']
```

The unit sets `LMBENCH_TIMEOUT=3600`. The runner's default is 900 s, which the
guest exceeds: it was cut off at 905 s having reached 73 of 109.

The remaining gaps are not this route's and are not fixed: `rpc-udp`/`rpc-tcp`
time out (`localhost: RPC: Timed out`, also seen on RISC-V), and `signal-prot`
is intermittent — it measured in one run and not the next, so treat `observed`
as 105-106 rather than a fixed number. `passed` stays false until those are
resolved; see
[the bring-up evidence](../../../docs/porting/evidence/2026-10-13-x86-debian-bringup/README.md).

The base root image must be one the builder repaired: `mke2fs -d` can leave the
block bitmap inconsistent, and a later write then lands on a block another inode
already owns. That is what made every Nix-linked binary die at startup here.

## Running it

```sh
make test_x86_debian_boot \
  X86_DEBIAN_KERNEL=target/osdk/asterinas-osdk-bin.elf \
  X86_DEBIAN_STAGE1_INITRAMFS=target/debian-x86/stage1/initramfs.cpio \
  X86_DEBIAN_ROOT_IMAGE=target/debian-x86/m6/rootfs/debian-root.ext2
```

`make test_x86_debian_tooling_unit` runs these scripts' own tests.

See
[`docs/porting/evidence/2026-10-13-x86-debian-bringup/`](../../../docs/porting/evidence/2026-10-13-x86-debian-bringup/README.md)
for the observed boot and the failures that are expected noise on this kernel.
