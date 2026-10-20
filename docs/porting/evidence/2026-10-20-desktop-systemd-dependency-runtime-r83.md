# M4 systemd dependency runtime boundary (r83)

- Gate: `desktop-systemd-dependency-runtime`
- Status: `failed`; the failure is an artifact-construction boundary, not a
  new kernel or Firefox result.

## Inputs

- rootfs manifest: `/mnt/mydisk/qemu-desktop-m4-r82-rootfs/rootfs-manifest.json`
- runtime receipt: `/mnt/mydisk/qemu-desktop-m4-r82-cow-stage1-120/result.json`
- serial log: `/mnt/mydisk/qemu-desktop-m4-r82-cow-stage1-120/desktop-m4.serial.log`

The run used a qcow2 write layer, a 120-second boot deadline, and no Firefox
scene.  Stage1 reached the systemd banner.  The precise activation failure was:

`basic.target: Wants dependency dropin /etc/systemd/system/basic.target.wants/asterinas-desktop-m4.service is not a symlink, ignoring.`

No `ASTERINAS_DESKTOP_M4_SESSION` marker followed it, and the gate classified
the run as `passed=false`, `reason=protocol`.

## Interpretation

The regular-file overlay was intentionally rejected by systemd; it cannot
stand in for the symlink that the corrected `build_rootfs.sh` now creates.
The runtime result therefore does not invalidate the source change in r82.  It
does establish that a real builder artifact containing the symlink is required
before the next activation run.

## Next gate

Build or materialize one artifact using the corrected builder (with a real
`basic.target.wants/asterinas-desktop-m4.service` symlink), then repeat the
same 120-second activation gate once.  Do not reuse this regular-file overlay
or start Firefox until the service-start marker is observed.
