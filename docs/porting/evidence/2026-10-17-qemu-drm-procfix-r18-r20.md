# QEMU DRM desktop handoff: proc/sysfs fix and bounded runtime replays

Date: 2026-10-17 (Asia/Shanghai)

This receipt covers the host/QEMU boundary only. It does not qualify the
physical board and it does not claim a virgl renderer or Firefox WebRender.

## Inputs

| Input | Path | SHA-256 |
| --- | --- | --- |
| kernel | `target/qemu-uboot/virgl-replay-20261017-transfer/fs-root/asterinas.booti` | `08c7acecb9374b86be24c9ae8f73d8b4d5de9d87f7a05b89c466d99c15bdb2f5` |
| U-Boot | `target/qemu-uboot/virgl-replay-20261017-transfer/u-boot` | `18c34ed87dd053ad012a0d3f8acef7445d40b0199c4da54ec04ba8cbba841710` |
| DTB | `target/qemu-uboot/virgl-replay-20261017-transfer/fs-root/qemu-virt.dtb` | `d6e8cf8a76188f7f247568c1df255ff699fefd40d74f2fe063b62ffa3381e3c2` |
| stage1 | `target/debian-riscv/stage1-procfix-20261017/initramfs.cpio` | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| root image | `target/debian-riscv/desktop-m4-cached-20261017-r2/rootfs/debian-root.ext2` | `55505a310e971c99da16b7d4e085406b8c7b08cdc961a38e1c12c4ed565cf97d` |
| manifest | `target/debian-riscv/desktop-m4-cached-20261017-r2/rootfs/rootfs-manifest.json` | `47015af3fe7e5f39ba7aa86c54e497ff895bd52d70d0171b79ac680b8379b103` |
| packages lock | `target/debian-riscv/desktop-m4-cached-20261017-r2/rootfs/packages.lock` | `d95fae625cd02c48c2cacc7bf179525b84e4022f2ca7f2aff4adb087877abded` |
| package checksums | `target/debian-riscv/desktop-m4-cached-20261017-r2/rootfs/source-metadata/package-checksums` | `6e012ae31950c555e04e29d23e0843bb6e1b18d828977a2e3c32515d67dfc6e8` |

The QEMU command used the GL-capable binary contract (`-display
egl-headless`, `-device virtio-gpu-gl-device`, no network) and the fixed
`/dev/vdb` root-device argument.

## Bounded results

### r18: stage1 proc/sysfs handoff

Raw receipt: `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r18/`.

The serial transcript contains the ordered handoff records:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-done action=dev-bind
DEBIAN_STAGE1_PROGRESS step=handoff-done action=proc-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=sysfs-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=api-directories
DEBIAN_STAGE1_PROGRESS step=handoff-done action=run-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=tmp-mount
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chroot
DEBIAN_STAGE1_PROGRESS step=handoff-done action=chdir
DEBIAN_STAGE1_PROGRESS step=handoff-enter action=exec
systemd 257.13-1~deb13u1 running in system mode
Detected architecture riscv64.
```

The stage1 native self-test passed after adding proc and sysfs to the systemd
handoff sequence. This closes the missing proc/sysfs handoff contract. The
runtime then stopped at the early systemd boundary; it emitted no desktop,
Xorg, virgl, pixel, or recovery marker. `result.json` records
`passed=false, reason=protocol`.

### r19: diagnostic logging

Raw receipt: `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r19/`.

The run used `systemd.log_level=debug systemd.log_target=console
systemd.show_status=1` with a 120-second host bound. It reached the kernel and
stage1 `handoff-enter action=exec`, but no systemd banner was emitted before
the host terminated QEMU. This is a diagnostic timeout, not evidence that the
guest reached or failed a particular systemd unit.

### r20: host preparation bound

Raw directory: `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r20/`.

The run was terminated before QEMU launch. The gate was still preparing its
writable root copy: the process had read roughly 8.8 GiB while validating and
hashing the 1 GiB ext2 image, and remained in uninterruptible filesystem I/O
for more than ten minutes. No serial log or guest result was produced. This
is a host preparation/throughput issue and is intentionally excluded from the
guest runtime verdict.

### r21: reuse-disk preparation bound

Raw directory: `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r21/`.

The host reused the r17 run disk through a hard link and rebuilt only the
64 MiB boot image. Input validation still performs full immutable-root hashing;
the process remained in filesystem I/O after reading about 1.3 GiB and was
stopped before QEMU launch. No guest serial output was produced. This confirms
that the dominant delay is the gate's repeated root-image validation, not the
GL backend or guest systemd.

## Gate verdict

| Gate | Result |
| --- | --- |
| stage1 proc/sysfs handoff ordering and native self-test | **PASS** |
| QEMU GL launch and device pairing | **PASS in prior host preflight; not relaunched in r20/r21** |
| systemd-to-desktop runtime | **OPEN**; r18 stopped after systemd banner |
| root-image preparation latency | **OPEN**; r20/r21 stopped before launch |
| `renderer=virgl`, pixels, Xorg, Firefox WebRender | **NOT CLAIMED** |
| physical board | **NOT TOUCHED** |

The next bounded step is to reduce the repeated root-image preparation cost or
reuse an already validated run disk, then capture early systemd unit progress
without changing the DRM/Xorg contract. No physical experiment is justified
until the QEMU guest emits the strict DRM milestones, virgl pixel evidence,
and recovery receipt.
