# Rootfs copy-on-write preparation (r80)

- Gate: `rootfs-cow-overlay`
- Scope: make the existing Debian gate able to use a qcow2 write layer over
  the authenticated raw ext2 image.  The default raw mode and QEMU contract
  remain unchanged.
- Status: `code-pass`; no Firefox or physical-board run was performed.

## Change

`--root-copy-mode raw` remains the default.  The opt-in `qcow2` mode creates a
small qcow2 layer whose raw backing path, format, and virtual size are checked
before publication.  QEMU receives `format=qcow2`; the source image SHA remains
the manifest-bound identity.  Launch still reconstructs the root path from the
pinned output descriptor.

This removes the full 2 GiB preparation copy from the opt-in path.  It does not
weaken source validation or change the default E1–E5 gates.

## Verification

| Check | Result |
| --- | --- |
| qcow2 overlay unit test | PASS; overlay 196,640 bytes, raw backing and format verified |
| QEMU argv format unit test | PASS |
| CLI default/opt-in mode test | PASS |
| `make test_riscv_debian_rootfs_unit` | PASS, 329 tests |
| Python compile, shell syntax, `git diff --check` | PASS |
| Real r79 raw image overlay probe | PASS; source SHA `43dae97c…`, temporary overlay removed |

## Runtime boundary

One bounded r80 M4 run used the new mode:

- receipt: `/mnt/mydisk/qemu-desktop-m4-r80-cow-stage1-120b/result.json`
- QEMU argv contains `format=qcow2` and the pinned
  `/proc/self/fd/.../debian-root.run.qcow2` path;
- the guest reached the complete Stage1 root handoff and the systemd banner;
- result: `passed=false`, `reason=protocol`, with no desktop marker before the
  120-second boot deadline.

This proves the COW layer can launch the guest and preserves the existing
desktop activation blocker; it does not satisfy E3.

The first r80 attempt exposed a path issue when `qemu-img` was asked to create
inside `/proc/self/fd`; that was corrected to create in the checked output
directory and launch through the pinned descriptor.
