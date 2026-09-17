# DRM acceptance protocol

This historical M1-M5 protocol remains a regression reference.
The current validation tiers, evidence requirements, and architecture exit
criteria are defined in [`VALIDATION.md`](VALIDATION.md).

Unified acceptance protocol for the DRM workstream (`track/drm`).
A single QEMU boot must prove three previously-independent kernel results
coexist and work together:

1. **DRM / KMS desktop** — Xorg's `modesetting` driver drives `/dev/dri/card0`
   (virtio-gpu), and systemd reaches `graphical.target`.
2. **ALSA audio** — a 440 Hz tone leaves the guest through `virtio-sound` and is
   decoded host-side (amplitude **and** pitch, not just a byte count).
3. **NetSurf browser** — the bundled local home page actually paints (colour
   histogram, not "no crash").

This is the **M5** acceptance. The M1–M4 milestone harnesses remain the
per-subsystem regression checks (see [Regression matrix](#regression-matrix)).

The result of a run is a single line: `=== DRM-M5: PASS (smp=N) ===`.

---

## Milestone map

| milestone | what it proves | harness | report |
|---|---|---|---|
| M1 | virtio-gpu 2D pipeline (`ATTACH_BACKING` etc.) | `tools/riscv/nixos/drm/boot_drm.py` | `tools/riscv/nixos/DRM-M1-report.md` |
| M2 | KMS ioctls + dumb-buffer mmap | `tools/riscv/nixos/drm/boot_drm_m2.py` | `tools/riscv/nixos/DRM-M2-report.md` |
| M3 | Xorg `modesetting` desktop | `tools/riscv/nixos/drm/boot_drm_m3.py` | `tools/riscv/nixos/DRM-M3-report.md` |
| M4 | hardware cursor (`MODE_CURSOR`/`2`) | `tools/riscv/nixos/drm/boot_drm_m4.py` | `tools/riscv/nixos/DRM-M4-report.md` |
| **M5** | **DRM + ALSA + NetSurf in one boot** | `tools/riscv/nixos/m5/{build_m5.sh,boot_m5.py}` | `tools/riscv/nixos/DRM-M5-report.md` |

---

## Prerequisites (one-time)

The integration guest is assembled from **three** source trees; the harness
reaches into two sibling trees for prebuilt userspace:

| artifact | source | env override |
|---|---|---|
| systemd desktop rootfs (Xorg + matchbox-wm + xpanel + pcmanfm + xterm + NetSurf GTK) | `$HOME/Program/asterinas-riscv/target/systemd-desktop/rootfs` | `DESKTOP_TREE` |
| DRM `modesetting_drv.so` + `libdrm.so` (riscv64 cross) | `$HOME/Program/asterinas-riscv/target/riscv-cross` | — (path derived from `DESKTOP_TREE`) |
| Alpine musl `aplay` + `alsa-lib` (unpacked `.d` APK dirs) | `$HOME/Program/asterinas-riscv-nixos/target/nixos/audio/alpine` | `ALSA_CACHE` |
| U-Boot + DTB seed | `/tmp/drm-m4/{u-boot,qemu-virt.dtb}` | — (copied to `/tmp/drm-m5`) |

Toolchain / host requirements:

- `qemu-system-riscv64` with `virtio-gpu`, `virtio-sound` (`wav` backend),
  `virtio-keyboard`, `virtio-tablet` (and `virtio-net` for `--net`).
- `riscv64-linux-gnu-gcc` (compiles the static `alsa-test` launcher).
- Kernel build prerequisites (see [Build](#build)): a tree-local `cargo-osdk`
  (the shared `~/.cargo/bin/cargo-osdk` has absolute paths baked into another
  sibling tree), `OSDK_TARGET_ARCH=riscv64`, `rust-objcopy`,
  `VDSO_LIBRARY_DIR`, and a dummy `test/initramfs/build/initramfs.cpio.gz`.

---

## Build

### 0. Kernel (only when the kernel changed)

From `kernel/`:

```bash
OSDK_TARGET_ARCH=riscv64 cargo osdk build --scheme riscv --features riscv_sv39_mode
```

This produces `target/osdk/aster-kernel-osdk-bin.Image`, which `build_m5.sh`
packs as `asterinas.booti`. Re-establish the local `cargo-osdk` symlink before
every build — concurrent sibling-tree sessions clobber it:

```bash
rm -f ~/.cargo/bin/cargo-osdk && ln -s /tmp/osdk-bin/bin/cargo-osdk ~/.cargo/bin/cargo-osdk
```

### 1. Integration initramfs + boot disk

```bash
bash tools/riscv/nixos/m5/build_m5.sh
```

This assembles `target/nixos/m5/initramfs.cpio` (desktop rootfs + modesetting
driver + libdrm + musl `aplay`/`alsa-lib` + the `alsa.service` oneshot + a
generated `sine.wav`), then re-packs `/tmp/drm-m5/boot.ext4` with
`asterinas.booti` + the initramfs + DTB. `--no-repack` skips the disk step.

---

## Run

```bash
python3 tools/riscv/nixos/m5/boot_m5.py
```

Useful flags:

| flag | default | purpose |
|---|---|---|
| `--net` | off | attach `virtio-net` (NetSurf favicon/link fetches) |
| `--settle-seconds` | `120` | wait after boot before the screendump (NetSurf paints slowly under TCG) |
| `--smp` | `1` | vCPU count |
| `--serial-log` | `/tmp/drm-m5/serial.log` | full serial transcript |
| `--screenshot` | `/tmp/drm-m5/shot.ppm` | screendump (PPM P6) |
| `--wav` | `/tmp/drm-m5/alsa-out.wav` | host WAV capture |

---

## Acceptance criteria

The runner prints three sections and a final verdict. **All three** must pass
for `PASS`:

### 1. DRM / desktop

Guest markers (from the serial transcript):

```
graphical-target OK · xorg-started OK · xorg-banner OK
```

Corroborated by the Xorg log lines:

```
(II) Loading /usr/lib/xorg/modules/drivers/modesetting_drv.so
(II) modeset(0): using default device
(II) modeset(0): Output Virtual-1 using initial mode 1280x800 +0+0
```

`modesetting` (not the bochs `fbdev` driver) must own the display.

### 2. ALSA audio

Guest marker `__ALSA_DONE__ __ALSA_PASS__` **and** the host-side WAV decode:

```
amplitude : RMS=… (min RMS 2000)
pitch     : … Hz (expect 440 +/- 12)
audible   : OK
```

The host decodes QEMU's `wav` backend output (RIFF header is repaired on the
fly), so a pass proves the PCM *left the guest* — not merely that `aplay`
exited 0.

### 3. NetSurf browser

The post-settle screendump histogram must contain the home page's signature
colours:

```
non-black: >0%  cream(#f4e8d0): >0  blue(#1a4f8b): >0
```

`cream == 0` **and** `blue == 0` (or a solid-black histogram) means the page
never painted — a FAIL even if `netsurf-started OK`.

Final verdict: `=== DRM-M5: PASS (smp=1) ===` (exit code 0).

---

## Known failure modes / gotchas

| symptom | cause | fix |
|---|---|---|
| every service dies `status=237/KEYRING` | keyring stub returns `EOPNOTSUPP` for `SETPERM`/`LINK`/`UNLINK` | kernel fix `905491522` / PR #43 must be in the tree |
| `__ALSA_*` markers never appear | journal socket (`/run/systemd/journal/stdout`) does not exist; `journal+console` drops output | `alsa.service` must write `StandardOutput=file:/dev/ttyS0` |
| solid-black screendump | NetSurf UI-resource load is slow under TCG | `--settle-seconds 120` (20 s is not enough) |
| initramfs fails to unpack / hangs | gzip inflate hangs on >16 MB inputs | pack **raw** `newc` cpio (no gzip), ~95 MB |
| initramfs clobbers the DTB | DTB at `0x8800_0000` sits under the ~95 MB initramfs | load DTB at `0x9000_0000` |
| `modeset(0): failed to get plane resources` | `DRM_MODE_PLANE_GETRESOURCES` unimplemented | non-fatal; Xorg falls back to ShadowFB (the forced path) |
| `WARN: framebuffer: Framebuffer not found` | no simple-framebuffer DTB node → no `/dev/fb0` | expected; the desktop drives DRM, not fbdev |

---

## Regression matrix

The M5 boot is the integration gate. Before it (and after any kernel change
that touches DRM), re-run the per-subsystem harnesses — the M5 runner covers
ALSA + NetSurf but only spot-checks the cursor indirectly:

```bash
python3 tools/riscv/nixos/drm/boot_drm_m4.py   # cursor: 4 virtio_gpu_update_cursor events, __DRM_PASS__
```

The M1–M4 harnesses are trace/marker-based and independent of the desktop
rootfs; they remain the fast pre-integration check.

---

## Where the artifacts live

| path | contents |
|---|---|
| `tools/riscv/nixos/m5/` | integration harness (build + boot + ALSA launcher + unit) |
| `tools/riscv/nixos/drm/` | M1–M4 harnesses + `xorg-modesetting.conf` |
| `tools/riscv/nixos/*-report.md` | milestone reports |
| `/tmp/drm-m5/` | independent boot disk (`u-boot`, `boot.ext4`, `qemu-virt.dtb`, `mon.sock`) |
| `target/nixos/m5/` | assembled rootfs + `initramfs.cpio` |
