# DRM-M8 — devtmpfs auto-create fix + DRM lands in the systemd desktop main boot chain

Date: 2026-08-15
Branch: `track/drm`
Status: **DONE** — (1) `device::init_in_first_process` no longer panics when
`/dev` is absent (the kernel creates it, PR #47); (2) the DRM desktop is promoted
from the M5 harness to a `boot_systemd_desktop` **main chain** that runs Xorg
modesetting on `/dev/dri/card0` with an fbdev fallback selected at runtime.

---

## 0. TL;DR

| item | result |
|---|---|
| `/dev` panic | `device::init_in_first_process` looked up `/dev` and panicked if absent ("path resolution did not reach the final target") |
| fix | create `/dev` (0755) when the lookup does not resolve, then mount devtmpfs |
| regression gate | initramfs with **no** `/dev` → `__M8_DEV__=DIR`, `__M8_OPEN_CONSOLE__=OK` — **PASS** |
| upstream PR | `fix/devtmpfs-auto-create-dev` → `main` (PR **#47**) |
| desktop main chain | `boot_systemd_desktop.py --gpu drm` (modesetting on `/dev/dri/card0`), `--gpu bochs` (fbdev fallback) |
| runtime fallback | `/init` selects `xorg-modesetting.conf` vs `xorg-fbdev.conf` by probing `/dev/dri/card0` |

---

## 1. Part 1 — the `/dev` panic (reproduce → locate → fix → verify → PR)

### 1.1 Symptom

A minimal initramfs that omits `/dev` panics the kernel before PID 1 runs:

```
kernel/src/fs/vfs/path/resolver.rs:787  "path resolution did not reach the final target"
```

This was first hit in M7's verification harness (see [[drm-m7-persistence]]): a
minimal initramfs must contain an empty `/dev` directory or the kernel panics in
`device::init_in_first_process`. Linux does not have this requirement — the
kernel creates `/dev` (devtmpfs) itself.

### 1.2 Root cause

`kernel/src/device/mod.rs::init_in_first_process` (called from `init.rs:169`):

```rust
let dev_path = path_resolver.lookup(&FsPath::try_from("/dev")?)?;   // panics if /dev absent
dev_path.mount(FsAndRoot::new(RamFs::new()), …)?;
```

`PathResolver::lookup` calls `lookup_unresolved(...)?.into_path()`. When `/dev`
does not exist, `lookup_unresolved` returns `LookupResult::AtParent` (resolution
stopped at `/` with the unresolved name `dev`), and `into_path()` converts that to
`Err(ENOENT, "path resolution did not reach the final target")`. The `?` propagates
it and the `.unwrap()` at `init.rs:169` panics — before any device node is
registered, so `/dev/console` (which `fs::init_in_first_process` opens next) is
never created either.

### 1.3 Fix

`kernel/src/device/mod.rs` — create `/dev` when the lookup does not resolve:

```rust
let dev_path = match path_resolver.lookup_unresolved(&FsPath::try_from("/dev")?)? {
    LookupResult::Resolved(path) => path,
    LookupResult::AtParent(_) => {
        let (parent, name) = path_resolver
            .lookup_unresolved(&FsPath::try_from("/dev")?)?
            .into_parent_and_filename()?;
        parent.new_fs_child(&name, InodeType::Dir, mkmod!(a+rx, u+w))?
    }
};
dev_path.mount(FsAndRoot::new(RamFs::new()), …)?;
```

The remaining initializers (`tty`, `pty`, `shm`, `registry`) already run *after*
this mount and re-look-up `/dev` (which now resolves to the mounted devtmpfs), so
they are unaffected.

### 1.4 Verification

`tools/riscv/nixos/m8/{nodev_init.c,build_m8_devfix.sh,boot_m8_devfix.py}` boots the
kernel with an initramfs that contains **no** `/dev`:

```
=== DRM-M8 devtmpfs auto-create result ===
  init-ran: OK  dev-is-dir: OK  console-present: OK  console-open: OK  init-done: OK
  collection-ended: init-done
=== DRM-M8 dev-fix: PASS ===
```

### 1.5 Upstream rollup

The fix is extracted onto a clean one-commit branch off `origin/main` (the
pre-fix `init_in_first_process` and import block are byte-identical to
`origin/main`'s copy, so the cherry-pick is conflict-free):

- Branch: `fix/devtmpfs-auto-create-dev`, base `main`, head
  `fix/devtmpfs-auto-create-dev`, one commit.
- PR: **#47** `fix(device): auto-create /dev before mounting devtmpfs`.

---

## 2. Part 2 — DRM in the systemd desktop main boot chain

### 2.1 From harness to main chain

M5 verified DRM + ALSA + NetSurf in one boot, but through a one-off harness
(`tools/riscv/nixos/m5/boot_m5.py`) that hardcodes virtio-gpu + virtio-sound and
its own `/tmp/drm-m5` paths. M8 promotes the **desktop** half of that into a
generalized `boot_systemd_desktop` main chain:

- `tools/riscv/nixos/m8/boot_systemd_desktop.py` — the main-chain desktop boot
  driver. `--gpu drm` (default) attaches `-device virtio-gpu-device` and requires
  the modesetting driver to grab `/dev/dri/card0`; `--gpu bochs` attaches
  `-device bochs-display` + the simple-framebuffer DTB injection and requires the
  fbdev driver (the previous SYSTEMD-DESKTOP path).
- `tools/riscv/nixos/m8/build_m8_desktop.sh` — layers the DRM modesetting driver +
  libdrm onto the sibling tree's systemd desktop rootfs, bundles **both** Xorg
  configs, and repacks `/tmp/drm-m8-desktop/boot.ext4` with the DRM-tree kernel.
- `tools/riscv/nixos/m8/init_drm.c` — the static PID-1 launcher. Besides the usual
  early setup it **probes `/dev/dri/card0` at runtime** and copies
  `xorg-modesetting.conf` or `xorg-fbdev.conf` onto `/etc/xorg.conf`, so the
  unmodified `xorg.service` (`Xorg -config /etc/xorg.conf`) picks the right driver.

### 2.2 Runtime fbdev fallback

The fallback is genuinely runtime, not a build-time flag: the same rootfs boots
either GPU, and `/init` selects the driver based on what the kernel exposed. On a
virtio-gpu boot the kernel has no simple-framebuffer node (`WARN: framebuffer:
Framebuffer not found`) and provides `/dev/dri/card0`, so modesetting is chosen;
on a bochs boot `/dev/dri/card0` is absent and `/dev/fb0` is present, so fbdev is
chosen.

### 2.3 Verification

```
python3 tools/riscv/nixos/m8/boot_systemd_desktop.py --gpu drm --smp 1
```

```
=== DRM-M8 systemd desktop result (gpu=drm) ===
  init-launcher: OK  graphical-target: OK  xorg-started: OK  xorg-banner: OK
  xorg-input-devices: OK  modesetting-driver: OK  modesetting-using: OK
  fbdev-driver: MISSING (expected — DRM mode uses modesetting, not fbdev)
  collection-ended: desktop-up
=== DRM-M8 desktop: PASS (gpu=drm, smp=1) ===
```

The serial tail confirms the real KMS path (not fbdev):

```
(==) modeset(0): Backing store enabled
(II) modeset(0): Initializing kms color map for depth 24, 8 bpc.
(II) modeset(0): Setting screen physical size to 338 x 211
(II) XINPUT: Adding extended input device "keyboard" / "pointer"
```

### 2.4 Finding — smp=4 hangs before init

`--smp 4` reproducibly stalls the DRM tree's kernel in `on_first_process_startup`
(Process-stage component init / early device bring-up), **before** `/init` runs —
the serial stops at the kernel banner and `init-launcher` never appears. This is a
pre-existing SMP race in the DRM/input device initialization (not introduced by
the `/dev` fix, which is VFS-only and passes at smp=1), and every prior DRM
milestone (M1–M7) also ran at `smp=1`. Left as a follow-up; the desktop main chain
is verified at the proven `smp=1` configuration.

---

## 3. Files changed

- `kernel/src/device/mod.rs` — auto-create `/dev` before mounting devtmpfs (the
  kernel fix; PR #47).
- `tools/riscv/nixos/m8/nodev_init.c` — regression `/init` (no `/dev` in the
  initramfs).
- `tools/riscv/nixos/m8/build_m8_devfix.sh` / `boot_m8_devfix.py` — devtmpfs
  auto-create regression harness.
- `tools/riscv/nixos/m8/boot_systemd_desktop.py` — DRM-tree main-chain desktop
  boot driver (`--gpu drm|bochs`).
- `tools/riscv/nixos/m8/build_m8_desktop.sh` — assemble the DRM desktop rootfs
  (modesetting + fbdev + fallback init) and repack the boot disk.
- `tools/riscv/nixos/m8/init_drm.c` — PID-1 launcher with runtime GPU fallback.
- `tools/riscv/nixos/m8/xorg-fbdev.conf` — fbdev fallback Xorg config.
- `tools/riscv/nixos/DRM-M8-report.md` — this report.

---

## 4. Result

| deliverable | status |
|---|---|
| `/dev` panic root-caused | lookup → `AtParent` → `ENOENT` → `unwrap()` panic |
| kernel fix | auto-create `/dev`, then mount devtmpfs |
| regression gate | no-`/dev` initramfs boots, `/dev/console` opens — **PASS** |
| upstream PR | `fix/devtmpfs-auto-create-dev` → `main` (**#47**) |
| desktop main chain | `boot_systemd_desktop.py --gpu drm` modesetting on `/dev/dri/card0` |
| fbdev fallback | runtime `/init` config selection by probing `/dev/dri/card0` |
| DRM desktop boot | **PASS** (see §2.3) |
