# QEMU desktop gate: root-init parameter diagnosis

The offline browser rootfs was materialized successfully and its ext2 label is
`ASTER_BROWSERWEB`.  A QEMU rootfs gate using the historical kernel, DTB,
U-Boot, and newly built stage1 initramfs reached stage1 but repeatedly reported
`result=no-match` for `/dev/vda` and `/dev/vdb`, then ended with
`DEBIAN_ROOTFS_FAIL reason=root-discovery-timeout`.

The serial transcript identifies the cause:

```
DEBIAN_STAGE1_PROGRESS step=start mode=interactive
```

The stage1 interactive mode intentionally accepts only the interactive root
label.  The U-Boot input used by this generic rootfs gate does not install the
browser desktop bootargs, so the `ASTER_BROWSERWEB` label is rejected.  Adding
`--root-init=systemd` to a temporary DTB did not change the result because this
U-Boot image overwrites the chosen bootargs during its boot script.

This is an artifact/boot-script contract failure, not evidence of a GMAC,
DRM, or Firefox failure.  The desktop QEMU path must use the guarded U-Boot
command runner, which explicitly installs the profile bootargs and records
their proof, before it can be used as the software-desktop canary.

No physical boot was attempted from this failed gate.

