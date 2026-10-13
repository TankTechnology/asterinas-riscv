# Booting a Debian root filesystem on the x86_64 Asterinas kernel

Before this change the x86_64 build could only boot an initramfs. There was no
Debian userspace on it, and the x86 CI job runs `bench_linux_and_aster.sh`
with a fixed job list — it has never used `make results`. This is a new
capability on x86 rather than a gap being closed.

The RISC-V workflow was already there, and most of it transfers: the rootfs
builder, its package contract, and the Stage1 root handoff are architecture
neutral. What did not transfer was the boot.

## The boot path

The RISC-V guest is started with `-kernel`/`-initrd`/`-append` under U-Boot.
x86 has no equivalent pre-UEFI handoff, but it does not need one: QEMU loads a
multiboot2 ELF itself. `-kernel` takes the Asterinas ELF — OSDK patches its
`e_machine` to `EM_386` for exactly this, because QEMU refuses an `EM_X86_64`
multiboot image — and `-initrd` becomes the first multiboot2 module, which the
kernel magic-sniffs as a plain cpio rather than gzip.

That means the Stage1 initramfs works unmodified. It finds the root disk by
reading the ext2/ext4 volume label out of each candidate's superblock, so the
Debian image may sit on any virtio-blk slot; here it lands on `/dev/vdd`.

```
DEBIAN_STAGE1_PROGRESS step=start mode=systemd
DEBIAN_STAGE1_PROGRESS step=probe-block device=/dev/vda … /dev/vdb … /dev/vdc
DEBIAN_STAGE1_PROGRESS step=probe-complete result=match
DEBIAN_STAGE1_PROGRESS step=root-found device=/dev/vdd
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=root-mount … dev-bind …
    api-directories … run-mount … tmp-mount … chroot … chdir … exec
systemd 257.13-1~deb13u1 running in system mode
Detected architecture x86-64.
Welcome to Debian GNU/Linux 13 (trixie)!
[  OK  ] Reached target multi-user.target - Multi-User System.
[  OK  ] Reached target graphical.target - Graphical Interface.
```

### Why not the OSDK scheme

`cargo osdk run` is the project's normal path, and it cannot express this boot:

- `[run.boot]` in `OSDK.toml` always appends `sh -l` to the init arguments,
  which the standard test initramfs consumes as a command to run. There is no
  CLI flag or scheme that removes them — `--init-args` appends, and a named
  scheme inherits the global arguments ahead of its own. Stage1's
  `parse_root_init` rejects any token it does not recognise, so a command line
  built by OSDK can never reach it.
- The `normal` scheme attaches three auxiliary conformance images, so it
  cannot boot a Debian disk on its own.

`tools/x86/debian/boot_debian.sh` therefore passes the QEMU command line
directly. It advertises `--print-argv` and `--print-append` so the command line
is asserted in tests without running QEMU: one drive, no `-bios`, no `-cdrom`,
and `init=/init -- --root-init=systemd --root-fs=ext4` in the append string.

## What had to change

**The Stage1 systemd handoff failed on a minimal initramfs.** `/newroot/run` is
a fresh tmpfs, and when the root label is not the M2 one the handoff binds the
initramfs's `/usr/lib/asterinas` into `/newroot/run/asterinas-tools` to lend
the root the desktop helper scripts. An initramfs built with `--minimal` has no
such directory, and binding an absent source fails, which ended the whole
handoff at `handoff-failed action=run-mount`. The bind is now skipped when the
source is absent; the helpers are an accessory, not a requirement.

**`systemd-journald.service` timed out.** It failed with `result 'timeout'`,
and because the journal socket is ordered before most of the boot, the start
job sat there while every dependent unit waited. The kernel cannot complete
systemd's `Type=notify` readiness handshake. The scratch disk now carries
`/etc/systemd/system/systemd-journald.service.d/asterinas-notify-compat.conf`
with `Type=simple` and `NotifyAccess=none`. This is a guest-side workaround, the
same shape the desktop work already needed for logind, and it is not a kernel
change.

**Log level.** At the default level the kernel prints a line per syscall and
`ldconfig.service` alone runs past 100 seconds of guest time; the boot only
proceeds at `loglevel=4`. The boot script defaults to that and says why.

### Architecture parameterization

The modules that freeze a rootfs identity assumed RISC-V in more places than
the surface suggested:

| Module | What assumed RISC-V |
| --- | --- |
| `contract.py` | The manifest architecture, the gate-package architecture filter, and the writer's own re-validation through the verifier's code path |
| `signed_sources.py` | The `Architectures:` field of `InRelease` and the apt `Packages` index paths |
| `build_rootfs.sh` | Installed the user-mode emulator unconditionally, so a native build ran `install` with an empty name |
| `build_stage1.sh` | The compiler and the default output directory; the probe's syscall numbers |
| `lmbench_native.py` | The GNU triple and the Nix `target` argument |

`stage1_probe.c` carried the RISC-V generic syscall numbers as constants and
now takes them from `<sys/syscall.h>`. On RISC-V the numbers are unchanged; the
probe *names* stay `syscall213`/`syscall272` because they are labels the megrez
probe, its validators and its recorded transcripts share.

## Non-fatal gaps observed

Each of these is reported by systemd and then worked around or ignored; none
stops the boot:

- `Failed to open netlink, ignoring: Protocol not available`
- `Failed to bump fs.file-max` / `fs.nr_open`: the sysctls do not exist
- `bpf-restrict-fs: BPF LSM hook not enabled in the kernel`
- `systemd-sysctl.service` fails: no `/proc/sys` tree to apply
- `dev-hvc0.device` times out after 90 s; `serial-getty@hvc0.service` then fails
  its dependency and no login prompt is offered on the console
- `Failed to find module 'autofs4'` / `'unix'`: no module loading

## Reproducing

```sh
# 1. A Debian root image for amd64.
tools/riscv/debian/rootfs/build_rootfs.sh --architecture amd64 \
  --profile systemd-ext4-m6-userspace --output-dir target/debian-x86/m6/rootfs

# 2. The Stage1 initramfs for amd64.
tools/riscv/debian/rootfs/build_stage1.sh --architecture amd64 --minimal \
  target/debian-x86/stage1/initramfs.cpio

# 3. An x86_64 kernel OSDK can hand to QEMU directly.
(cd kernel && cargo osdk build --target-arch x86_64 --boot-method vmm-direct \
  --initramfs ../../target/debian-x86/stage1/initramfs.cpio)

# 4. Boot it.
tools/x86/debian/boot_debian.sh \
  --kernel target/osdk/asterinas-osdk-bin.elf \
  --initramfs target/debian-x86/stage1/initramfs.cpio \
  --root-image target/debian-x86/m6/rootfs/debian-root.ext2 \
  --output target/x86-debian/boot.transcript \
  --wait-for "Reached target multi-user.target"
```

`make test_x86_debian_boot` runs the same thing from the Makefile.
