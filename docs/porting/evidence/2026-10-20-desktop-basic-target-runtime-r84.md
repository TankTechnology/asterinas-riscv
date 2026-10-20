# M4 basic-target runtime with real symlink (r84)

- Gate: `desktop-basic-target-runtime`
- Status: `failed`; this is a bounded QEMU result, not a Firefox or physical
  board result.

## Inputs and artifact

- rootfs: `/mnt/mydisk/qemu-desktop-m4-r84-rootfs/debian-root.ext2`
- rootfs manifest: `/mnt/mydisk/qemu-desktop-m4-r84-rootfs/rootfs-manifest.json`
- runtime result: `/mnt/mydisk/qemu-desktop-m4-r84-cow-stage1-120b/result.json`
- serial log: `/mnt/mydisk/qemu-desktop-m4-r84-cow-stage1-120b/desktop-m4.serial.log`
- symlink verification: `debugfs stat` reports `Type: symlink` and target
  `../asterinas-desktop-m4.service`.

## Result

The guest reached the full Stage1 handoff and systemd banner.  The previous
r83 diagnostic (`basic.target ... is not a symlink, ignoring`) disappeared,
confirming that the wants entry is now a real symlink.  However, the bounded
120-second run emitted no `ASTERINAS_DESKTOP_M4_SESSION` or
`ASTERINAS_DESKTOP_DEVICE_ACCESS` marker and ended with:

```text
passed=false
reason=protocol
```

The serial tail contains no service-start failure; systemd stops after its
banner.  The remaining boundary is therefore target/service activation before
the M4 helper, rather than the symlink type or the logind/udev dependency.

## Next diagnostic

Do not repeat this same desktop run.  Add one early, independent systemd
activation marker (before M4/Xorg) or enable bounded systemd console logging to
distinguish “default target not entered” from “unit job not dispatched”.  Only
after that boundary is visible should the Firefox scene be considered.
