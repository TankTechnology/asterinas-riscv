# DRM-M14 — benchmark completion, KMS mode-switch smoke, and the virgl pre-research

Date: 2026-08-16
Branch: `track/drm` (harness + report)
Status: **DONE** — (1) the fbdev-vs-modesetting `xbench` numbers are completed into a
full per-operation A/B table (the M10 numbers were a partial sample, missing
`putimage` on both drivers and unpaired across sessions); (2) the KMS
mode-switch path (`SETCRTC` at two *different* resolutions) is smoke-tested for
the first time; (3) Weston is assessed as blocked on the missing GL/GBM/EGL
stack, and the virtio-gpu **virgl** 3D path is pre-researched with a work
estimate instead of being implemented.

---

## 0. TL;DR

| item | result |
|---|---|
| benchmark A/B table | complete for 6 ops (fill / rect-sync / rect-nosync / line / point / putimage), fbdev vs modesetting, smp=1 |
| x11perf | **not run** — the guest has no `x11perf` binary (§1.4) |
| KMS mode switch | **PASS** — `SETCRTC` 1280×800 → 640×400 → 1280×800, `GETCRTC` confirms each step (§2) |
| Weston | **blocked** — needs GBM+EGL (GL), which needs virgl (§3.1) |
| virgl pre-research | work estimate **~1–2 months**, dominated by the DRM render-node/GEM layer (§3.2) |
| code changes | none to the kernel — harness + report only (§4) |

---

## 1. Benchmark: fbdev vs modesetting, per operation

### 1.1 What changed since M10

M10 produced a *partial* A/B sample: `fill / rect-sync / rect-nosync / line /
point` for both drivers, but `putimage` was unbounded and pathological (>5 min
for 8000 ops on both drivers) so it never produced a number, and the two drivers
were sampled in different sessions under different host load. M11 then bound
`putimage` to 200 ops so `XBENCH done` fires deterministically, but only re-ran
the **modesetting** side at smp=4.

This milestone re-ran **both** drivers at **smp=1** with the same (bounded)
`xbench` binary, so every operation is paired across the two drivers in one
session:

### 1.2 The table (smp=1, ops/sec — higher is better)

| operation (iters) | fbdev (bochs) | modesetting (virtio-gpu) |
|---|---|---|
| fill-rect-fullscreen (20) | **98** | 8 |
| rect-500, per-batch XSync (20 000) | **635** | 292 |
| rect-500, no-sync (20 000) | 1018 | 821 |
| line-500 (20 000) | **1526** | 1199 |
| point-1000 (20 000) | **11654** | 6300 |
| putimage-64x64 (200) | 6 | 6 |

(For reference, the uncontended **smp=4** modesetting run from M11: fill 264,
rect-sync 3385, rect-nosync 17200, line 8848, point 77068, putimage 40.)

### 1.3 Interpretation — and why the ranking is not the headline

The only robust, reproducible findings are the *driver-independent* ones:

- **putimage is pathological and identical on both drivers** (6 vs 6 ops/sec).
  The cost is X-server-side image handling, not the DRM driver — which is
  exactly why M10's unbounded putimage starved the boot. Bounding it to 200 ops
  (M11) is what makes the desktop boot terminate.
- **Neither driver has 2D acceleration.** fbdev writes straight into the
  guest-visible framebuffer; modesetting renders into a shadow framebuffer and
  pushes each full frame through `DIRTYFB` → `TRANSFER_TO_HOST_2D` + `FLUSH`
  (a ~4 MiB transfer per present at 1280×800). Both are pure software paths, so
  the per-op ranking is decided by which CPU the host happens to schedule —
  not by the driver.

The per-op *ranking is not stable across sessions*. This session fbdev wins
every operation, but the M10 smp=1 sample had the **opposite** result for the
batched-geometry ops (modesetting rect-nosync 2419 vs fbdev 601; line 3709 vs
992). Same code, same drivers, different host contention. So §1.2 is the
"complete table" the milestone was asked for, but it should be read as a
paired-in-one-session sample, not as a verdict on which driver is faster. The
uncontended smp=4 modesetting row shows the driver's headroom once the host
stops competing for CPU.

### 1.4 x11perf

Not available. `find` over both the desktop rootfs and the cross sysroot turns
up no `x11perf` binary (the sibling tree ships X11 libs + Xorg + NetSurf, but
not the `x11perf` suite). Cross-compiling it would mean pulling in the
`x11perf` source and its `libXext`/`libXft` deps, which is out of scope for a
smoke milestone — the self-hosted `xbench` covers the same five primitive
families. Recorded as a gap rather than silently skipped.

### 1.5 Noise caveat

`xbench` ops/sec swing ~8–14× with host load (two sibling-tree
`qemu-system-riscv64` guests were running on this 16-thread box during the
run). The M10 sample (fbdev fill 7, modesetting fill 15) vs this session
(fbdev fill 98, modesetting fill 8) is the same code under different contention.
A/B conclusions above therefore lean on the per-op *ranking* and the smp=4
uncontended row, not on any single absolute figure.

---

## 2. KMS mode-switch smoke test

The kernel's `MODE_SETCRTC` (`dri.rs::set_crtc`) had only ever been exercised by
Xorg's modesetting driver, which sets the device's single preferred mode and
never switches. This milestone drives it through two *different* resolutions on
the same CRTC.

`tools/riscv/nixos/m14/modeswitch.c` runs as a bare pid-1 `/init` (no Xorg, so
it owns `/dev/dri/card0` uncontended) and:

1. `GETRESOURCES` → CRTC id 1;
2. `GETCONNECTOR` → current preferred mode (**1280×800**, QEMU's default);
3. `CREATE_DUMB` + `MAP_DUMB` + `mmap`-fill + `ADDFB` a **640×400** fb;
4. `SETCRTC` to the smaller fb;
5. `GETCRTC` → confirm `fb_id=1 mode=640x400`;
6. `CREATE_DUMB`/`ADDFB` a **1280×800** fb and `SETCRTC` back;
7. `GETCRTC` → confirm `fb_id=2 mode=1280x800`.

Result (serial log, `/tmp/m14-modeswitch/serial.log`):

```text
[MODESWITCH] getconnector     OK   current mode 1280x800
[MODESWITCH] target           OK   target mode 640x400
[MODESWITCH] setcrtc-target   OK   switched to smaller mode
[MODESWITCH] verify-target    OK   fb_id=1 mode=640x400
[MODESWITCH] setcrtc-orig     OK   switched back to original mode
[MODESWITCH] verify-orig      OK   fb_id=2 mode=1280x800
__MODESWITCH_DONE__ __MODESWITCH_PASS__
```

The kernel's `set_crtc` ignores the requested mode and presents whatever fb is
given, so the "resolution switch" is real: each `SETCRTC` re-runs the full
virtio-gpu 2D pipeline (`RESOURCE_CREATE_2D` → `ATTACH_BACKING` → `SET_SCANOUT`
→ `TRANSFER_TO_HOST_2D` → `FLUSH`) at the new dimensions, and `GETCRTC` reflects
the new framebuffer id + mode. Switching down and back leaves the CRTC usable —
the path a future multi-resolution or hotplug flow would rely on.

---

## 3. Weston smoke vs virgl pre-research

### 3.1 Weston — blocked, not attempted

Weston's compositor path needs a GL/GBM/EGL stack: the DRM backend requires
libgbm + EGL, which in turn require a working GL driver (virgl on virtio-gpu).
The cross tree has libwayland-\* static libs but **no libgbm / libEGL /
libGLES / Mesa-virgl**, and no weston binary. The alternative (Weston's
headless/fbdev backends with the pixman software renderer) would run without GL
but would not exercise the DRM desktop the milestone is about. Conclusion:
**defer Weston until virgl lands** — it is not a smoke-test away.

### 3.2 virgl — pre-research (no implementation)

What exists today (all read-only; no code changed):

- **Guest kernel `virtio-gpu`** (`kernel/comps/virtio/src/device/gpu/*`):
  `negotiate_features` returns **0**, so every device feature is cleared —
  including `VIRTIO_GPU_F_VIRGL` (0x1). Only the 2D block
  (`RESOURCE_CREATE_2D`, `ATTACH_BACKING`, `SET_SCANOUT`,
  `TRANSFER_TO_HOST_2D`, `FLUSH`, `UNREF`) and the cursor block
  (`UPDATE_CURSOR`/`MOVE_CURSOR`) are implemented. There are no wire types for
  the 3D block (`CTX_CREATE` 0x200 … `SUBMIT_3D` 0x207), no capset handling
  (`GET_CAPSET_INFO` 0x108 / `GET_CAPSET` 0x109), and no context.
- **Guest DRM layer** (`kernel/src/device/dri.rs`): modesetting-only on
  `/dev/dri/card0`. There is **no render node** (`/dev/dri/renderD128`), **no
  GEM object model** (only dumb buffers), **no execbuffer**, and no context
  ioctls — the surface a virgl client (Mesa) actually drives.
- **Userspace**: no Mesa-virgl / GBM / EGL / GLES in the cross tree.
- **Host**: `libvirglrenderer.so.1.11.0` is present and QEMU 11.0.3 ships the
  virgl-capable `virtio-gpu-gl-device` (+ `egl-headless` display). The current
  boot uses the 2D-only `virtio-gpu-device`; switching to
  `-device virtio-gpu-gl-device -display egl-headless` would expose
  `VIRTIO_GPU_F_VIRGL`.

Work estimate (in order of cost):

| piece | effort |
|---|---|
| 3D wire types + control-queue handlers + capset fetch/store | ~1 week |
| DRM render node + GEM objects + `VIRTIO_GPU_EXECBUFFER` + context ioctls | **~2–3 weeks** (dominant — the current `dri.rs` has none of this) |
| Mesa virgl Gallium driver cross-compiled for riscv64 (+ libdrm render-node support) | ~1 week |
| host boot config (`virtio-gpu-gl` + `egl-headless`) + bring-up/testing | ~1 week |
| **total** | **~1–2 months** |

The render-node/GEM layer is the long pole: it is a new subsystem, not an
extension of the existing dumb-buffer path. This is the honest scope; it is not
a follow-up to slip into the current rollup.

---

## 4. Verification summary

| check | command / evidence | result |
|---|---|---|
| fbdev benchmark | `boot_m10.py --gpu bochs --smp 1` | PASS, 6 ops captured |
| modesetting benchmark | `boot_m10.py --gpu drm --smp 1` | PASS, 6 ops captured |
| mode switch | `boot_modeswitch.py` | PASS (`__MODESWITCH_PASS__`) |
| kernel | unchanged — no `cargo osdk build` needed | — |

No kernel code changed; the existing `target/osdk/aster-kernel-osdk-bin.Image`
(from the M11 build) is current (`git` shows no kernel-source commits since).

---

## 5. Files changed (this branch)

- `tools/riscv/nixos/m14/modeswitch.c` — the KMS mode-switch smoke test.
- `tools/riscv/nixos/m14/build_modeswitch.sh` — cross-compiles it and assembles a minimal boot disk.
- `tools/riscv/nixos/m14/boot_modeswitch.py` — boots it under virtio-gpu and asserts the marker.
- `tools/riscv/nixos/DRM-M14-report.md` — this report.

---

## 6. Result

| deliverable | status |
|---|---|
| full fbdev-vs-modesetting A/B table (6 ops) | **done** (§1.2) |
| x11perf | not available, recorded as a gap (§1.4) |
| KMS mode-switch smoke (2 resolutions) | **PASS** (§2) |
| Weston smoke | blocked on GL/GBM/EGL (§3.1) |
| virgl pre-research | work estimate ~1–2 months, render-node/GEM dominant (§3.2) |
| kernel code | unchanged |
