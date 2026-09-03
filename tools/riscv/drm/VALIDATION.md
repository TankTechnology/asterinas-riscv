# DRM validation and architecture plan

Date: 2026-09-03

## Scope

The validated target is Asterinas's single-scanout virtio-gpu driver on QEMU.
It covers the primary and render nodes, legacy and atomic KMS, dumb buffers,
GEM/FLINK/PRIME sharing, cursor commands, virgl contexts and transfers,
asynchronous fences, Mesa DRI3, Xorg glamor, and an Xfce desktop.

The kernel also has a firmware-framebuffer KMS fallback for systems without virtio-gpu.
It exposes `card0` as `simpledrm`, allocates non-contiguous dumb buffers, copies BGRX8888/XRGB8888 rows into the fixed firmware scanout, reports no hardware cursor, and does not expose `renderD128`.
This path has passed RISC-V and x86_64 OSDK compile checks, but still requires a Megrez board run before it is considered operational evidence.

This scope does not claim every Linux DRM feature.
Unsupported capabilities must be rejected instead of advertised as working.
Native EIC7700 modesetting and GPU acceleration remain later backends.
The firmware fallback intentionally leaves the mode, HDMI link, clocks, and resets under firmware control.

## Validation model

The normal test loop does not boot Linux for exhaustive differential testing.
It combines three independent sources of evidence:

1. Linux UAPI and virtio specification contracts;
2. real unmodified userspace such as libdrm, Mesa, Xorg, and Xfce; and
3. guest-visible results plus QEMU virtio-gpu command traces.

A small set of disputed or high-risk behaviours may be captured once on Linux as versioned golden results.
Those samples are refreshed only when the UAPI baseline changes or when an authoritative specification cannot settle a semantic question.

## Test tiers

### Per change: under ten minutes

- compile-time UAPI size, offset, ioctl-number, flag, and constant assertions;
- Rust kernel tests for bounded arithmetic and state invariants;
- static C cross-compilation with `-Wall -Wextra -Werror`;
- negative ioctl cases for invalid pointers, ids, handles, flags, and lengths;
- focused lifecycle tests for every bug fix; and
- `git diff --check` plus targeted formatting.

### DRM change: bounded guest gates

- M17 atomic KMS and property gate;
- raw virgl creation, transfer, pixel, fence, and double-buffer gate;
- GEM/FLINK/PRIME import/export and close-order gate;
- M22 device-wide lifetime counters and repeated GEM/PRIME/context/fence close;
- concurrent control-queue clients; and
- one complete Mesa DRI3/Xorg/Xfce virgl boot.

### Firmware-framebuffer change: physical board gate

- current HEAD boots on Megrez through the existing RAM-only framebuffer DTB handoff;
- `/dev/dri/card0` exists and reports `simpledrm`, while `renderD128` is absent;
- `modetest` enumerates the fixed 1920x1080 mode and reports zero cursor dimensions;
- `SETCRTC`, `PAGE_FLIP`, and `DIRTYFB` produce the distinct HDMI patterns A, B, and C in order;
- Xorg modesetting with llvmpipe reaches the desktop without using the fbdev driver; and
- serial logs contain no framebuffer mapping, KMS, page-flip, or kernel errors.

Before the board is available, `make test_riscv_drm_firmware_preboard` is the required migration gate.
It cross-builds a deterministic static RISC-V initramfs and tests the Megrez board-session contract without opening a serial device.
The probe checks `simpledrm`, absence of `renderD128`, the advertised dumb/shadow/cursor capabilities, the fixed 1920x1080 preferred mode, two dumb buffer mappings, `ADDFB2`, `SETCRTC`, `PAGE_FLIP`, and `DIRTYFB`.
Its negative self-tests reject a virtio driver identity, an unexpected render node, wrong capabilities or mode, and an ioctl failure.
This is compile/static evidence; only the physical `firmware-drm` session may produce runtime evidence.

The physical serial transcript must contain these probe records in order:

```text
DRM_FIRMWARE_VERSION driver=simpledrm render-node=absent
DRM_FIRMWARE_CAPS dumb=1 prefer-shadow=1 cursor=0x0
DRM_FIRMWARE_MODE connector=connected mode=1920x1080 preferred=1
DRM_FIRMWARE_DUMB buffers=2 format=XRGB8888 mmap=pass
DRM_FIRMWARE_PRESENT stage=setcrtc pattern=A ioctl=pass
DRM_FIRMWARE_PRESENT stage=page-flip pattern=B ioctl=pass
DRM_FIRMWARE_PRESENT stage=dirtyfb pattern=C ioctl=pass
ASTERINAS_DRM_FIRMWARE_R1_READY
```

The serial markers only prove successful ioctl returns.
Physical evidence must also include HDMI video or an operator record showing A, B, and C in order;
otherwise the board run is incomplete.

The executable build and guarded board command are maintained in [`../README.md`](../README.md#firmware-drm-pre-board-gate).

### Nightly

- repeated Xorg and GL context start/stop cycles;
- randomized GEM, framebuffer, PRIME, context, and fence lifetimes;
- selected `modetest`, `kmscube`, IGT, and Piglit cases that match the claimed
  feature set;
- one-hour render and close/reopen soak; and
- resource counters returning to their pre-test baseline.

### Release candidate

- at least ten complete virgl desktop boots without a panic, hang, renderer
  fallback, command-stream error, or leaked resource;
- selected Linux golden behaviours refreshed for master/auth, atomic KMS,
  PRIME, render-node permissions, and fences;
- interaction and `glmark2-es2` A/B results for fbdev, DRM/llvmpipe, and
  DRM/virgl; and
- an eight-hour stress run on an otherwise idle host.

## Coverage matrix

| Boundary | Fast evidence | Integration evidence | Stress evidence |
|---|---|---|---|
| UAPI layout | static assertions | unmodified libdrm calls | malformed ioctl corpus |
| master/auth | focused C cases | Xorg primary-node open | competing clients and exit |
| KMS objects | M17 enumeration | `modetest`/Xorg | repeated mode discovery |
| atomic state | TEST_ONLY and invalid pairing | `kmscube`/Xorg | commit, close, and signal races |
| GEM/PRIME | raw import/export | DRI3 shared buffers | randomized close order |
| virgl context | raw command stream | Mesa direct rendering | concurrent clients and process kill |
| virgl resource | target/mip/box/backing bounds | Mesa texture and buffer traffic | malformed transfer corpus |
| fences | poll and NOWAIT | Mesa submissions | pending-fence close and timeout |
| presentation | pixel checks | Xfce screenshots | frame-time and long-run checks |
| partial atomic update | inherited-state ktests | full modeset then FB-only flips | repeated multi-buffer flips |

Every functional gate must assert both success markers and the absence of
known failure markers.
For virgl these include `Illegal resource`, rejected `CREATE_OBJECT`, illegal
command buffers, renderer fallback to llvmpipe, panic, and an unfinished fence.

## Machine-readable evidence

Each retained run should record:

- Git commit and dirty-tree state;
- kernel, initramfs, rootfs, DTB, and U-Boot hashes;
- QEMU, Mesa, libdrm, and virglrenderer versions;
- complete QEMU arguments and display backend;
- architecture, page-table mode, CPU count, and memory size;
- serial log and host trace hashes;
- structured pass/fail checks; and
- timing distributions rather than only one FPS value.

JSON is the authoritative result format.
Markdown reports summarize the JSON and link its retained artifact identity.

## Architecture driven by validation

Refactoring follows observed boundaries instead of creating speculative
abstractions:

1. Move Linux wire structs and constants out of the device state module into a
   dedicated UAPI module, guarded by layout tests.
2. Keep globally unique, typed KMS objects and parse atomic requests into a
   validated transaction before changing state.
   Property identity is a typed kernel enum rather than a userspace-visible
   display string.
   The first transactional implementation now validates the complete proposed
   single-pipeline state, publishes it only after hardware success, and keeps
   legacy SETCRTC/PAGE_FLIP state coherent with atomic property queries.
3. Separate generic DRM file, GEM, PRIME, KMS, and event state from the
   virtio-gpu command backend.
4. Express GEM handles, host resources, context attachments, and fences as
   explicit lifetime-owning types with rollback on failed userspace copies.
   Property blobs follow the same rule: per-file ownership is distinct from
   references held by committed KMS state.
5. Establish and document one lock order for file state, KMS state, resource
   creation, context state, and the control queue.
6. Add resource accounting and trace points before attempting page-flip or
   buffer-copy performance optimizations.

The current property-blob store enforces 64 KiB per blob, 4 MiB in total, and
256 live blobs. Device-wide fdinfo accounting now exposes GEM owners, host
resources, virgl contexts and attachments, retained fences, backend ownership,
scanout/cursor state, and each deferred-cleanup layer. M22 asserts that all
reclaimable counters return to a quiescent baseline after staged GEM, PRIME,
context, and fence teardown.
It also checks mapping-owned and host-owned DUMB span lifetimes.
M22 requires live pool usage to return to baseline, verifies reuse after both
DRM and PRIME `munmap`, and runs 4,200 cycles whose cumulative allocation
exceeds pool capacity.
It now also validates the Gallium resource contract before host submission:
target-specific creation geometry, mip-level transfer geometry, backing
offsets, plus exact byte extents for the common linear 32-bit formats. Other
format layouts remain host-validated by virglrenderer. The real Mesa gate
performs one complete atomic modeset followed by three plane `FB_ID`-only flips, proving that
committed KMS properties are inherited across partial updates.
The current method and evidence are recorded in
[`../nixos/DRM-M22-report.md`](../nixos/DRM-M22-report.md).

The scanout backend boundary is now consumed by virtio-gpu, a fake ktest backend, and the firmware framebuffer implementation.
The fixed framebuffer layout validator covers the physically observed Megrez 1920x1080 BGRX8888 contract and rejects incompatible formats or undersized strides.
This does not replace the physical HDMI gate above.

## Exit criteria for the virtio-gpu DRM foundation

The foundation is ready for mainline integration when:

1. supported UAPI layouts and advertised capabilities have explicit tests;
2. atomic requests are transactional and use correct object/property binding;
3. GEM, PRIME, context, backing, and fence stress returns all counters to the
   baseline;
4. the selected `modetest`, `kmscube`, IGT, and Piglit suites pass;
5. raw virgl, the four-frame Mesa/GBM/atomic gate, and the full
   direct-rendering Xfce gate pass repeatedly;
6. page-flip and fence events are neither lost, duplicated, nor issued before
   the implementation's documented completion point;
7. performance results include frame-time percentiles and stall counts; and
8. RISC-V kernel tests build and run in CI.

Native EIC7700 register programming remains gated on a separate, source-led
clock/reset/MMIO/interrupt contract and physical recovery plan.
