# E3 systemd-sysctl local debug gate design

Date: 2026-10-20
Status: approved
Scope: E3, QEMU only

## Goal

Make the unmodified `systemd-sysctl.service` complete successfully and reach
`sysinit.target` within a bounded QEMU boot. This is a local debugging gate on
the path that currently blocks the standard systemd startup sequence; it does
not start Xorg or Firefox and does not claim E3 success.

Stop condition:

- the serial log contains `Reached target sysinit.target`;
- `systemd-sysctl.service` is started/finished, not `[FAILED]`;
- `/proc/sys/kernel/pid_max` reads `4194304`;
- `/proc/sys/kernel/core_pattern` reads `core`.

## Frozen inputs

The r128 probe is the failure baseline. Its recorded inputs are:

| Input | r128 value | Candidate |
|---|---|---|
| kernel | `54dd701c273b296c783269973bb1328abcecc6c0a0aea2a3c14bd13e8a6badad` (no longer on disk) | `/mnt/mydisk/aster-kernel-pidmax-corepattern-fix-r138.Image`, sha256 `6d83c89b8281c2b381a0e1b4534851e455cc4f2770370ce2957e7135715df2c5` |
| U-Boot | `47516422aea981a45eb3da6de786d356dbd9059c96bd9fc11d8e3ba28aec7210` | `target/qemu-uboot/cache/u-boot-build/u-boot`, same hash |
| DTB | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` | `target/qemu-uboot/debian-root/qemu-virt.dtb`, same hash |
| Stage1 | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` | `target/debian-riscv/stage1-procfix-20261017/initramfs.cpio`, same hash |
| root image | `5d7e10e83e14b617505c091cac7ca320d08154d60ba0e6299deed6406a84bffc` | `/mnt/mydisk/asterinas-firefox-rootfs-20261020-capab-ab-r122c/debian-root.ext2`, same hash |
| rootfs manifest | `f06e1088517d405c50a9fec7734995dc8af54910bb5b0e01a2629ddd4aa52e51` | same manifest |

All inputs except the kernel match r128 byte-for-byte. The r138 candidate is a
single-variable kernel replacement. The source delta is the `/proc/sys`
pid_max/core_pattern write support already present in the working tree.

The r128 kernel is no longer retained. The baseline failure is anchored by the
r128 serial log and the static rootfs contract, not by a new pre-fix run. No
attempt is made to rebuild the old kernel for this gate.

## rootfs probe overlay

Use the existing content-addressed `dev_overlay.py` mechanism on the frozen root
image. The overlay adds two files:

1. `/usr/lib/asterinas/sysctl-probe.sh`
2. `/etc/systemd/system/systemd-sysctl.service.d/90-asterinas-probe.conf`

The drop-in overrides only the command and console routing:

```ini
[Service]
ExecStart=
ExecStart=/usr/lib/asterinas/sysctl-probe.sh
StandardOutput=console
StandardError=console
```

The original unit's `ConditionPathIsReadWrite=/proc/sys/net/`,
`Before=sysinit.target`, and `TimeoutSec=90s` remain in effect. If the
condition fails, the probe is skipped and that is recorded as the finding.

The probe script runs each step under `timeout 15` and prints a
`SYSCTL_PROBE` marker with the step name, return code, and elapsed
milliseconds:

1. read `/proc/sys/kernel/pid_max`;
2. read `/proc/sys/kernel/core_pattern`;
3. write `4194304` to `/proc/sys/kernel/pid_max`, then read it back;
4. write `core` to `/proc/sys/kernel/core_pattern`, then read it back;
5. run the real `/usr/lib/systemd/systemd-sysctl` separately for
   `/usr/lib/sysctl.d/10-coredump-debian.conf` and
   `/usr/lib/sysctl.d/50-pid-max.conf`;
6. run the real binary once with no file arguments;
7. read a deliberately missing `/proc/sys/kernel/asterinas_probe_missing` as
   a negative control;
8. exit 0 so the probe itself cannot block `sysinit.target`.

## Boot and host runner

Reuse the existing bounded QEMU contract: generic QEMU `virt`, four harts,
2 GiB, `-nic none`, no display, U-Boot plus two virtio-blk devices.

Boot arguments target the stop condition directly:

```text
console=ttyS0 loglevel=4 systemd.unit=sysinit.target init=/init -- --root-init=systemd
```

`sysinit.target` is the exact target under test, so the desktop and Firefox
units are never enqueued and no `systemd.mask=` arguments are needed. r128 used
`basic.target`; that difference is intentional and does not affect
`systemd-sysctl.service`, which is ordered before `sysinit.target`.

The host runner must:

- validate every input hash before launch;
- materialize the overlay from the frozen base image;
- launch one bounded boot (120 s default);
- capture serial, lifecycle, result, and input hashes;
- parse `SYSCTL_PROBE` markers;
- stop after one run unless the failure is a host-side launch failure.

## Evidence and classification

Write a curated evidence page:

`docs/porting/evidence/2026-10-20-systemd-sysctl-probe-r136.md`

It contains a table of step, return code, elapsed time, and interpretation.
Raw serial and lifecycle files remain local and untracked.

Classification:

| Observation | Interpretation |
|---|---|
| all steps rc=0 | the kernel fix works at the syscall level; proceed to S2 |
| core_pattern read/write returns ENOENT | missing procfs entry; current implementation direction is correct |
| pid_max write returns EOPNOTSUPP/EINVAL | write path is incompatible; fix by exact errno |
| any step rc=124 | that step is the hang point; locate the kernel path before adding keys |
| direct writes rc=0 but the real binary rc!=0 | the failure is in systemd-sysctl or its configuration, not the kernel |
| no `SYSCTL_PROBE begin` marker | the unit's `ConditionPathIsReadWrite=/proc/sys/net/` failed or the drop-in was not loaded; classify before changing the kernel |

## Verification ladder

- S1 (this gate): probe overlay on the r138 candidate kernel; record exact
  per-step behavior.
- S2: remove the overlay, boot the same inputs with the original
  `systemd-sysctl.service`, and require `Reached target sysinit.target`.
  This is the stop condition for this design.
- S3: resume the Firefox short probe / E3 work only after S2 passes. It is not
  part of this gate.

## Guardrails

- One variable per run: no kernel rebuild between S1 and S2; only the overlay
  is removed.
- Same-boundary failure twice: write a `blocked` receipt and stop; do not run
  a third time.
- Do not edit `STATUS.md` manually; regenerate it with `verify.py`.
- Do not touch the physical board, Xorg, or Firefox in this gate.
- Raw logs stay local; only the curated Markdown page is tracked.

## Known risks

- The r128 kernel is not retained. The baseline is a serial-log plus
  static-contract anchor, not a rerun.
- The r138 kernel includes other uncommitted working-tree changes. The probe
  isolates the two procfs paths, but S2 is an integration check.
- If S2 still fails, the next variable is the exact S1 step, not an additional
  sysctl key.
