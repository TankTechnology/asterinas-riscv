# Device model adoption on `main` (shared-code step B1)

Date: 2026-10-28

## Why

The goal is one implementation of each device class for x86_64 and RISC-V, so
that architecture adaptation shrinks to the places that are genuinely
architecture specific. Today the two trees differ in where the shared code
lives:

* `main` has `kernel/src/device/**`, a monolithic device/sysfs layer inside the
  kernel, plus `kernel/comps/*` components that sit *below* the kernel and
  depend only on `ostd`.
* `feat/drm-virtio-gpu` (the source of #203) has `kernel/core/comps/*`, among
  them `aster-device`: a complete device model (buses, classes, drivers, and
  the `/sys` view built from them). Its DRM, audio, USB and simpledrm
  components are written against it.

`aster-device` is the one large feat-only component that does **not** depend on
`aster-core` (the kernel-as-a-library crate): its dependencies are
`aster-systree`, `aster-util`, `component`, `device-id`, `ostd` and `spin`,
which all exist on `main`. That makes it the natural first piece to move: it
can sit below the kernel exactly like the other `kernel/comps` crates, so no
kernel API layer is needed to adopt it.

## What this change does

1. **Forwards `kernel/comps/systree` to feat's version.** `aster-device` is
   written against it. The differences are a strict evolution of `main`'s
   systree, not a fork in another direction:

   * `SysNode::node_attrs` returns `Arc<SysAttrSet>` instead of `&SysAttrSet`.
     An attribute set is immutable; a node whose attributes change publishes a
     new set instead of editing the current one, so a reader may keep what it
     got. This is what lets the device model rebuild attributes (udev-style
     `uevent`) without racing readers.
   * `SysAttrSetBuilder` allocates attribute IDs with `id-alloc`, gains
     `remove`, `from_set` (keeping the IDs of surviving attributes) and
     reports bad names / exhausted IDs from `build()` instead of silently
     truncating. `SysAttrSet::CAPACITY` becomes 255, the actual size of the
     `u8` ID space (`1 << 8` used to allow id 255 to wrap into the next
     attribute).
   * Node and attribute names are validated (`is_valid_name`), and
     `Error::InvalidName` is added; the kernel maps it to `EINVAL`.
   * `relative_path()` computes symlink targets the way Linux's sysfs does,
     and `ObjFields<T: ?Sized>` allows object fields for trait objects.
   * Tests for all of the above (`kernel/comps/systree/src/test.rs`).

2. **Adds `kernel/comps/device`** (`aster-device`, 26 files, ~4.5k lines),
   copied from `feat/drm-virtio-gpu` with only formatting changes, registered
   in `Cargo.toml`, `kernel/Cargo.toml`, `Components.toml` and the workspace
   dependency table.

3. **Wires the kernel's sysfs view through the device model.** The device model
   owns `/sys/devices`, `/sys/dev`, `/sys/bus` and `/sys/class`, so
   `kernel/src/device/sysfs.rs` no longer adds those directories to the tree
   root by hand:

   * `/sys/class/tty` and `/sys/class/graphics` are attached with
     `aster_device::register_legacy_class_dir`, `/sys/bus/platform` with
     `register_legacy_bus_dir`, and each `/sys/dev/char/<major>:<minor>` node
     with `register_legacy_dev_entry`.
   * `kernel/src/security/tsm_mr.rs` uses
     `aster_device::register_legacy_virtual_node("misc", ...)` for
     `/sys/devices/virtual/misc/tdx_guest` instead of building the
     `devices/virtual/misc` path itself (the FIXME there asked for exactly
     this), which also stops it from colliding with the device model's own
     `/sys/devices`.

   The subtree the DRM nodes need is unchanged: `device/subsystem`,
   `device/uevent`, `device/drm/` and the node's own `uevent` are all still
   built and attached the same way.

4. **Adapts the two kernel call sites** that borrowed an attribute set
   (`kernel/src/fs/utils/systree_inode.rs`): the directory-entry iterator now
   keeps the `Arc<SysAttrSet>` alive while it yields entries, and lookups bind
   the set to a local before borrowing an attribute from it.

## Verification

Build (x86_64 microvm scheme; the host's installed `cargo-osdk` was built from
another worktree with `OSDK_LOCAL_DEV=1` and bakes that path, so an OSDK built
from this tree was used):

```
cargo osdk build --target-arch x86_64 --scheme microvm   # ok
```

Cross-arch build (RISC-V, `riscv_sv39_mode`, RISC-V QEMU scheme):

```
cargo osdk build --target-arch riscv64 --scheme riscv \
    --features riscv_sv39_mode                            # ok
```

The device crate is architecture neutral: it takes no `cfg(target_arch)`
branch, so the same source builds for both.

Tests:

```
KTEST_CRATE_DIR=kernel/comps/systree   passed=16 failed=0
KTEST_CRATE_DIR=kernel/comps/device    passed=17 failed=0
./tools/ktest.sh (full suite)          passed=804 failed=0 (33 crates)
```

The full suite is the gate that matters here: it builds the kernel with
`--cfg ktest`, which is the only configuration that compiles the sysfs test
module, and it exercises every `/sys` consumer in the tree against the ported
systree.

Runtime: `main` cannot boot the Debian image (`main-kernel-cannot-boot-debian`),
so the topology was checked with `tools/x86/sysfs/sysfs_probe.c` as the `/init`
of a minimal initramfs. It mounts sysfs and prints what the device model built;
the log is in
`docs/porting/evidence/2026-10-28-device-model/sysfs-probe-x86.log`:

```
PROBE_LIST /sys: . .. devices bus class dev fs kernel block
PROBE_LIST /sys/dev/char: .. . 226:0 226:128
PROBE_LIST /sys/class: .. . tty graphics
PROBE_LIST /sys/bus: .. . platform
PROBE_LIST /sys/devices: .. . virtual
PROBE_LINK /sys/dev/char/226:0/device/subsystem -> ../../../../bus/platform
PROBE_READ /sys/dev/char/226:0/device/uevent: DRIVER=virtio_gpu
MODALIAS=virtio:d00000010
PROBE_READ /sys/dev/char/226:0/uevent: MAJOR=226
MINOR=0
DEVNAME=dri/card0
DEVTYPE=drm_minor
PROBE_LIST /sys/dev/char/226:0/device/drm: .. . card0 renderD128
PROBE_STAT /sys/class/tty/tty0 ok
PROBE_STAT /sys/class/graphics/fb0 ok
```

i.e. the device model creates the four roots, the DRM character nodes are
still where libdrm looks for them, the class and bus views survive, and
`/sys/devices` is the model's own tree instead of an empty duplicate.

## What is still missing

This is step B1 of the shared-core plan; it moves the model, not the drivers.

* The DRM nodes are still registered through the *legacy* entry points. Turning
  them into a real bus/class registration (`register_drm_device` in feat's
  `kernel/core/src/device/drm_sysfs.rs`) needs the DRM driver to live in a
  component, which is step B5 (`aster-drm`), and that crate depends on
  `aster-core`.
* `audio-hda`, `usb-xhci`, `drm-virtio-gpu` and `simpledrm` likewise stay in
  `kernel/src` until the kernel-services API layer exists for the parts they
  use (files, VM objects, time, events, credentials).
* `aster-device`'s device-node hooks are what will replace the per-subsystem
  `/dev` creation once those components move; on `main` they are unused so
  far.
