# M4 systemd activation follow-up (r79)

- Gate: `desktop-systemd-activation-r79`
- Scope: one bounded host-side materialization attempt for the M4 service
  dependency change. No guest boot, Firefox, network, serial write, physical
  board, or hardware MMIO action was performed.
- Status: `interrupted-host-copy`

## Intended change

The r79 overlay restores the M4 service's activation prerequisites
(`local-fs.target`, `dbus.service`, `systemd-udevd.service`, and
`systemd-logind.service`) while retaining the ordered-session safety boundary:
no `PAMName`, `TTYPath`, `StandardInput=tty`, or framebuffer access before the
provider phase.

## Bounded attempt

The following materialization was started once from the previously qualified
r78 image:

```text
python3 -m tools.riscv.debian.rootfs.dev_overlay materialize \
  --base-dir /mnt/mydisk/qemu-desktop-m4-r78-rootfs \
  --spec /mnt/mydisk/m4-r79-overlay/overlay.json \
  --output-dir /mnt/mydisk/qemu-desktop-m4-r79-rootfs
```

The image copy child (`cp --reflink=auto --sparse=always`) remained in
uninterruptible filesystem wait on `/mnt/mydisk` for more than twelve minutes
(`D`, `folio_wait_bit_common`).  It eventually published the prepared image,
but the subsequent gate snapshot copied the same 2 GiB image again and stayed
in the same wait.  The gate process was then terminated before guest launch;
`lifecycle.json` remains `phase=validate,status=running`, so no r79 runtime
result is valid.  The published image is usable only after its manifest
binding is checked; no partial private file is an input.

The interrupted gate's private 2 GiB temporary root copy was removed from the
r79 output directory.  The remaining `boot.ext4` and `lifecycle.json` are
diagnostic artifacts and are not a passing runtime pair.

Published r79 input hashes:

```text
debian-root.ext2          43dae97ca4d7896fe07dc4bf0e7ac059d0621bc560a403c277e5bd2cd0c82917
rootfs-manifest.json      9d2e5fbb81cd9f64626b68c256e8c09c659dda52e61c8d74ed90340054ac3709
dev-overlay-manifest.json 4c477d40c5bde21ac5d823b4e3e6bc0fdda7638b3cc844d633a3b73c6f3a76e7
```

## Code and static verification

The source/test change itself remains covered by:

| Check | Result |
| --- | --- |
| `make test_riscv_debian_rootfs_unit` | PASS, 327 tests |
| `python3 -m compileall -q tools/riscv/debian/rootfs tools/riscv/tests` | PASS |
| `bash -n tools/riscv/debian/rootfs/*.sh tools/riscv/*.sh` | PASS |
| `git diff --check` | PASS |
| reference scanner with `refactor-deletions.json` | PASS, 574 candidates inventoried |

The host I/O boundary is the only open item for r79.  Do not retry the same
copying gate until the rootfs snapshot path is changed to reuse a qualified
image; this receipt does not claim desktop success.
