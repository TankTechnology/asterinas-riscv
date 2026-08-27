# DRM validation and architecture plan

Date: 2026-08-27

## Scope

The current target is Asterinas's single-scanout virtio-gpu driver on QEMU.
It covers the primary and render nodes, legacy and atomic KMS, dumb buffers,
GEM/FLINK/PRIME sharing, cursor commands, virgl contexts and transfers,
asynchronous fences, Mesa DRI3, Xorg glamor, and an Xfce desktop.

This scope does not claim every Linux DRM feature.
Unsupported capabilities must be rejected instead of advertised as working.
Native EIC7700 display and GPU support remains a later backend.
Work on it begins after the virtio-gpu implementation reaches the exit criteria below.

## Validation model

The normal test loop does not boot Linux for exhaustive differential testing.
It combines three independent sources of evidence:

1. Linux UAPI and virtio specification contracts;
2. real unmodified userspace such as libdrm, Mesa, Xorg, and Xfce; and
3. guest-visible results plus QEMU virtio-gpu command traces.

A small set of disputed or high-risk behaviours may be captured once on Linux
as versioned golden results.
Those samples are refreshed only when the UAPI baseline changes or a semantic
question cannot be settled from an authoritative specification.

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
- concurrent control-queue clients; and
- one complete Mesa DRI3/Xorg/Xfce virgl boot.

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
| fences | poll and NOWAIT | Mesa submissions | pending-fence close and timeout |
| presentation | pixel checks | Xfce screenshots | frame-time and long-run checks |

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

The backend boundary is considered useful only after both the virtio-gpu tests
and a small fake-backend state test can consume it.
This avoids designing the later Megrez interface before its hardware contract
is available.

## Exit criteria for the virtio-gpu DRM foundation

The foundation is ready for mainline integration when:

1. supported UAPI layouts and advertised capabilities have explicit tests;
2. atomic requests are transactional and use correct object/property binding;
3. GEM, PRIME, context, backing, and fence stress returns all counters to the
   baseline;
4. the selected `modetest`, `kmscube`, IGT, and Piglit suites pass;
5. raw virgl and the full direct-rendering Xfce gate pass repeatedly;
6. page-flip and fence events are neither lost, duplicated, nor issued before
   the implementation's documented completion point;
7. performance results include frame-time percentiles and stall counts; and
8. RISC-V kernel tests build and run in CI.

Only after this gate should work begin on a firmware-framebuffer DRM backend or
native EIC7700 display/GPU support.
