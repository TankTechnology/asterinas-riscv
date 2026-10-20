# Current Stage1 handoff evidence (r75)

- Gate: `desktop-stage1-current-artifact`
- Scope: one bounded headless QEMU boot; no Firefox, GL, network, serial write,
  or physical-board access.
- Status: `boundary-pass`

## Artifact identity

| Input | SHA-256 |
| --- | --- |
| stale `stage1-procfix-20261017/initramfs.cpio` | `ce7fb996e9a6ac48160d81c29347a68d88e99204adcc147051df76f645031c84` |
| rebuilt `target/debian-riscv/stage1-current-r75/initramfs.cpio` | `e03ab1b48f1715d727517ff7c63c203211d05de09dc063793335523e38efd019` |
| r75 root image | `1fa2a486453f72fbb56d109a5045d421ceca806c6f402f5848f6824c288c1a3e` |

The rebuilt archive is 1,109,504 bytes and the stale archive is 1,107,968
bytes.  The runtime receipt binds the rebuilt SHA above, so this run did not
silently reuse the stale Stage1 artifact.

## Runtime result

- Output: `/mnt/mydisk/qemu-desktop-m4-r75-stage1`
- Lifecycle: `status=failed`, `reason=protocol`
- Bounded boot timeout: 60 seconds
- Result: `docs/porting` does not claim E3 pass from this run.

Observed serial sequence:

```text
DEBIAN_STAGE1_PROGRESS step=handoff-done action=root-mount
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

This closes the previous Stage1-to-systemd handoff boundary seen in r73.  The
remaining boundary is after systemd starts: no
`ASTERINAS_DESKTOP_M4_*` service marker appeared before the deadline.  The
serial log also records `Failed to find module 'autofs4'` and `Failed to find
module 'unix'`; these are guest module-discovery diagnostics and require a
separate systemd/desktop investigation.

## Next gate

Use this exact rebuilt Stage1 identity for the next short systemd service
probe.  Do not rerun the 900-second Firefox scene until a desktop readiness
marker is observed with a bounded service-only probe.
