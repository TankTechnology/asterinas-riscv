# Stable Desktop and GPU Acceleration Implementation Plan

> **Status source of truth:** [`2026-10-14-desktop-gpu-acceleration-status.md`](2026-10-14-desktop-gpu-acceleration-status.md)
> records runtime evidence and the next gate. The checkboxes below are aligned
> with that board; a host fixture or parser test never substitutes for a
> guest or physical receipt.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the Asterinas desktop use a verified DRM/modesetting path with a software fallback, then enable QEMU virgl acceleration and only afterward qualify Firefox WebRender and the physical board.

**Architecture:** Keep firmware `fb0` as a recoverable provider and use virtio-gpu DRM as the QEMU provider. Expose `/dev/dri/card0` for modesetting and `/dev/dri/renderD128` for render clients; negotiate virgl only when QEMU and its GL backend prove they can provide it. Every handoff has a pixel hash, ordered serial markers, bounded completion waits, and a recovery receipt.

**Tech Stack:** Rust kernel/virtio-gpu, DRM KMS ioctls, QEMU U-Boot runner, Mesa/Xorg modesetting, EGL/GLES probes, Python gate classifiers, static RISC-V init programs. For QEMU 10.2 the GL display backend is `egl-headless`; it does not take a `gl=on` suboption.

---

## Current evidence and boundaries

The 2D DRM scanout gate and Xorg modesetting validator already pass in QEMU. The latest Xorg evidence produces a real screenshot and pixel hash, but reports `llvmpipe`; this is software rendering. A GL-capable QEMU 10.2.1 has now been built in the dedicated builder image and passes the host capability preflight. A current guest virgl parameter gate also passes (`3d=1`, virgl capset `0x2`, context/resource/backing/submit/fence receipts), but no Xorg or Firefox acceleration claim follows from that gate. No physical-board MMIO, serial write, reboot, or GPU probe is part of this plan until the guest display gates pass.

The immediate blockers before claiming an accelerated desktop are deliberately explicit: run the current guest with the GL-capable QEMU, fix the DRM-only rootfs handoff so it does not require `/dev/fb0`, expose the DRM device topology Mesa/libdrm reads from sysfs, use the modesetting driver's `kmsdev` option, and bind a real guest `about:support` report to the Firefox WebRender classifier. Existing host fixtures and parser tests do not count as those runtime receipts.

### Files and responsibilities

* `kernel/src/device/dri.rs`, `kernel/src/device/dri/backend.rs`, `kernel/src/device/dri/prime.rs`, `kernel/src/device/dri/fence.rs`: DRM card/render nodes, GEM/PRIME, scanout and fence contracts.
* `kernel/comps/virtio/src/device/gpu/device.rs`: virtio-gpu 2D and virgl control queue, context/resource/submit/transfer operations.
* `tools/riscv/qemu_uboot_devices.py`, `tools/riscv/qemu_uboot_commands.py`, `tools/riscv/qemu_uboot_profiles.py`: closed QEMU device and profile contracts.
* `tools/riscv/drm/*_gate.py`, `*_init.c`, `build_*.sh`: guest probes, transcript classifiers and bounded evidence.
* `tools/riscv/debian/rootfs/desktop_drm_gate.py`, Xorg configuration in the Debian rootfs: DRI3/modesetting and renderer selection.
* `tools/riscv/drm/egl-pixel-probe.py`, `gles-pixel-probe.py`, `qemu_scanout_contract.py`: deterministic pixel and screenshot checks.

### Task 1: Preserve and verify the provider boundary (complete)

**Files:** `tools/riscv/drm/scanout_gate.py`, `tools/riscv/drm/xorg_modesetting_gate.py`, `tools/riscv/tests/test_drm_scanout_gate.py`, `tools/riscv/tests/test_drm_xorg_modesetting_gate.py`.

- [x] Keep firmware `fb0` and virtio-gpu 2D as separate registered device contracts.
- [x] Require ordered `SETCRTC → transfer → flush` markers and QMP screenshot hashes.
- [x] Inject missing `SETCRTC`, transfer, and flush records; require fail-closed results and a clean retry that replaces stale evidence.
- [x] Require Xorg `driver=modesetting`, DRI3 `/dev/dri/card0`, fixed 1280×800 geometry, and a nontrivial pixel hash; reject fbdev-only sessions.

Run:

```bash
make test_riscv_drm_scanout_unit test_riscv_drm_xorg_modesetting_unit
```

### Task 2: Complete the render-node/GEM contract

**Files:** `kernel/src/device/dri.rs`, `kernel/src/device/dri/prime.rs`, `tools/riscv/drm/render_node_gate.py`, `tools/riscv/drm/gem_gate.py`, `tools/riscv/tests/test_drm_render_node_gate.py`, `tools/riscv/tests/test_drm_gem_gate.py`.

- [x] Build a kernel exposing `/dev/dri/card0` and `/dev/dri/renderD128`; the current QEMU desktop receipt records driver name `virtio_gpu`.
- [x] Run the render-node permission split: render ioctls succeed on `renderD128`, modesetting ioctls are refused there, and card control still succeeds (`2026-10-15-qemu-drm-render-node-current.md`).
- [x] Run a current runtime GEM probe with PRIME export/import; `2026-10-16-qemu-drm-gem-prime-enomem.md` proves two independent card files, PRIME export/import, stale-fd rejection, and ordered recovery.
- [x] Add a bounded allocation/exhaustion case so a long-lived desktop returns `ENOMEM` instead of wrapping the bump allocator; the same runtime receipt records the fixed 15-success/16th-refused sequence, with kernel ktests covering the arithmetic boundary.
- [x] Make host-resource lifetime follow the device-wide GEM reference count in `DriHandle::drop`, `RMFB`, GEM close and PRIME-sharing paths; the current kernel ktests cover non-final/final release and the two-file runtime probe is closed by `2026-10-16-qemu-drm-gem-prime-enomem.md` (no independent `prime.rs` ktest is claimed).

Run:

```bash
make test_riscv_drm_render_node_unit test_riscv_drm_gem_unit
```

### Task 3: Make virgl submission safe before enabling acceleration

**Files:** `kernel/comps/virtio/src/device/gpu/device.rs`, `tools/riscv/drm/virgl_param_gate.py`, `tools/riscv/drm/virgl_host_preflight.py`, `tools/riscv/tests/test_drm_virgl_param_gate.py`, `tools/riscv/tests/test_drm_virgl_host_preflight.py`.

- [x] Keep `VIRTIO_GPU_F_VIRGL`/`VIRTIO_GPU_F_CONTEXT_INIT` negotiation explicit and register a GL QEMU profile that pairs `virtio-gpu-gl-device` with QEMU's `egl-headless` backend (`egl-headless` itself enables GL; `gl=on` is not a valid suboption there).
- [x] Align `EXECBUFFER` with the Debian libdrm 2.4.124 32-byte ABI and pin the ioctl number with compile-time assertions.
- [x] Place the response after the complete variable-length 3D request; the old fixed 1024-byte offset overlapped commands larger than 992 bytes.
- [x] Reject command lengths that overflow the protocol field, require the returned fence flag and exact fence id, and bound control/cursor completion waits.
- [x] Run a host preflight before any real virgl launch; missing QEMU, EGL, or the GL device must publish a failed receipt without starting a guest.
- [x] Add guest probes for capset, context, resource, backing, submit, transfer, and fence with command sizes 992 and 993 bytes; the current QEMU receipt records both transfer directions and both boundary sizes.
- [x] Keep `RESOURCE_FLUSH` on the virtio-gpu 2D control-queue path: the ordered Task 1 scanout receipt covers SETCRTC → transfer-to-host-2D → flush. Virgl 3D has no separate context-stream `RESOURCE_FLUSH` command, so the gate does not invent one.
- [x] Inject context, submit, and fence failures in a real GL-QEMU run and retain a bounded fail-closed receipt in `docs/porting/evidence/2026-10-16-qemu-virgl-fault-matrix.md`.
- [x] Run the current guest parameter gate once with the GL-capable QEMU and retain its ordered serial receipt; this is a prerequisite result, not the completed desktop gate.

Run:

```bash
make test_riscv_drm_virgl_unit
```

### Task 4: Provide a real GL-capable QEMU environment

**Host prerequisites:** QEMU built with OpenGL/GBM/virglrenderer, Mesa/EGL libraries, and a matching libc. The executable must pass the preflight before use.

- [x] Build a matching QEMU GL variant in the dedicated Ubuntu 24.04 builder; do not use the external binary that requires unavailable `GLIBC_2.38`.
- [x] Verify `-display help` contains `egl-headless` and `-device help` contains `virtio-gpu-gl-device`.
- [x] Run the 3D control profile and GL profile; the plain profile reports `3d=0` and refuses submission, while the GL profile reports the virgl capset, nonempty caps, resource/backing, submit and completion fence. Transfer directions and the 992/993-byte boundary are covered by `docs/porting/evidence/2026-10-16-qemu-virgl-transfer-boundary.md`; context/submit/fence recovery is covered by `docs/porting/evidence/2026-10-16-qemu-virgl-fault-matrix.md`.
- [x] Save QEMU version, configure flags, Mesa version, and gate receipts under `docs/porting/evidence/` (`2026-10-14-qemu-virgl-host-preflight.md`, `2026-10-15-qemu-virgl-current.md`).

No result from a QEMU `virtio-gpu-device` run is allowed to satisfy this task; that device is only 2D.

### Task 4.5: Make DRM discoverable to Mesa/libdrm (current bounded phase)

**Files:** `kernel/src/device/sysfs.rs`, `kernel/src/device/dri.rs`.

- [x] Publish `/sys/class/drm/card0` and `renderD128` as links to one canonical
  `/sys/devices/virtual/virtio/virtio0/drm/` node, and publish matching
  `/sys/dev/char/226:*` links.
- [x] Add read-only `dev` and `uevent` attributes, a virtio parent with
  `driver`/`subsystem` links, and stable virtual-GPU PCI compatibility fields
  (`PCI_SLOT_NAME`, vendor/device IDs) required by libdrm's virtio parser.
- [x] Keep the topology behind a `virtio_gpu` provider check so firmware-only
  `simpledrm` boots do not claim a false virtio device.
- [x] Add a ktest-level relative-link contract for the class, canonical and
  dev/char paths.
- [x] Install a bounded `libdrm.so.2` probe that calls `drmGetVersion()` and
  `drmGetDevice2()` on `renderD128` before the desktop gate accepts Xorg.
- [x] Add the parent `device/drm/subsystem` link required by libdrm's
  `drmGetDevice2()` path; accept its documented PCI-compatibility bus result
  for virtio-mmio only with an explicit virtio transport marker.
- [x] Re-run the GL desktop after this topology change and require Xorg
  `modesetting + glamor + virgl`; the current v2 QEMU receipt has the
  sysfs/libdrm markers, `renderer='virgl'`, an X11 screenshot hash, and a
  separate recovery receipt.

The current evidence for this phase is recorded in
`docs/porting/evidence/2026-10-14-qemu-drm-sysfs-topology.md`. It is a QEMU-only
stage and does not authorize board MMIO, serial writes or rebooting.

### Task 5: Mesa/Xorg accelerated desktop gate

**Files:** Debian rootfs Xorg configuration and `tools/riscv/drm/egl-pixel-probe.py`, `tools/riscv/drm/gles-pixel-probe.py`, `tools/riscv/tests/test_drm_xorg_modesetting_gate.py`.

- [x] Start Xorg with the modesetting driver and DRI3 against `card0`; the current QEMU receipt reports `renderer='virgl'` and uses the pinned modesetting configuration.
- [x] Reject `llvmpipe`, `softpipe`, missing EGL, or a missing render node even if the screen is visually correct; the runtime classifier now fails closed on software-renderer strings and has regression coverage.
- [x] Draw fixed-color probe regions through EGL/GLES and capture a deterministic X11 pixel hash; the current `egl-headless` path uses guest X11 capture because it has no host scanout surface.
- [x] Inject failures at `SETCRTC`, transfer, flush, context creation, submit, and fence completion; each must stop the gate and allow software fallback or a clean reboot without a hung control thread. The three 2D legs are recorded in `docs/porting/evidence/2026-10-16-qemu-drm-scanout-fault-matrix.md`.

### Task 6: Firefox and daily-use qualification

**Files:** Debian Firefox launch profile, `tools/riscv/debian/rootfs/browser_web_*`, and a new fixed-scene browser gate.

- [ ] Launch Firefox with WebRender enabled only after the accelerated Xorg gate passes; capture `about:support` renderer and compositor fields.
- [ ] Use deterministic local HTML for scroll, CSS transform, canvas, and a short video frame sequence; record screenshot hash and frame-time histogram.
- [ ] Fail if Firefox selects `llvmpipe`, if the render node disappears, or if a browser restart loses the management console.
- [ ] Keep the existing software path as the fallback qualification and report the two paths separately.

### Task 7: Physical board qualification, after QEMU is green

The physical EIC7700 board is a separate provider. QEMU virgl accelerates through the host GPU and does not exercise the board's PowerVR GPU. Board acceleration requires a real PowerVR DRM driver, firmware, GEM/submit/fence ABI, and matching Mesa userspace; it cannot be obtained by installing Firefox or by passing a QEMU virgl gate.

- [ ] First boot the existing stable desktop with watchdog and management-chain evidence; do not combine network, USB, DRM and GPU experiments.
- [ ] Qualify firmware `fb0` and DRM modesetting as separate one-shot boots with a fresh nonce, UID 0, boot-id, serial reopen and RockOS fallback receipt.
- [ ] Only after a board DRM driver exists, run one fixed-pixel hardware-render test, then one Xorg modesetting session. Any timeout stops the run and waits for the watchdog; no automatic retry.
- [ ] Preserve the software fbdev fallback permanently until the board GPU path has independent recovery evidence.

### Acceptance

The desktop acceleration goal is complete only when all of the following are true:

1. QEMU 2D scanout, DRM modesetting and recovery gates pass.
2. QEMU render-node/GEM/virgl gates pass with a GL-capable QEMU and a renderer string other than `llvmpipe`.
3. Xorg glamor and deterministic EGL/GLES pixel hashes pass, including fault injection and recovery.
4. Firefox reports WebRender on the virgl path and passes the fixed local scene gate.
5. The physical board has an independent DRM/GPU driver and passes one-shot pixel and recovery gates.

Until item 5, the correct claim is “QEMU virgl accelerated prototype” (once QEMU Task 4 passes), not “EIC7700 hardware acceleration.”
