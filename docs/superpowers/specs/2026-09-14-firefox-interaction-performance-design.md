# Firefox Interaction Performance Design

**Date:** 2026-09-14
**Status:** Accepted for implementation

## Goal

Make the existing RISC-V Firefox desktop predictably responsive without depending on
unfinished DRM work.  The resulting system must retain the firmware-framebuffer
fallback, measure input and navigation latency with bounded tests, and expose a narrow
display-provider boundary that the DRM team can adopt in a later PR.

## Context and evidence

The physical Megrez system can boot Debian, start Xorg through `fbdev`, launch Firefox,
receive keyboard and pointer input, and reach an HTTPS page through the development-host
proxy.  The remaining defect is performance: local input and page transitions feel slow.

Historical browser evidence separates at least two costs.  Main-document transfer can
finish well before `DOMContentLoaded`, while many page resources take tens of seconds.
Older Firefox startup evidence also records long Marionette command latency without an
external navigation.  Therefore the design treats display/input latency, browser
execution, and network progress as separate boundaries.

An older Xserver bring-up report documents an unconditional full-screen shadow copy,
but that patch lived only under `target/`.  The current Debian rootfs installs packaged
`xserver-xorg-core` and `xserver-xorg-video-fbdev`.  No optimization may assume the old
patch is present: the running Xorg provenance and refresh behavior must be measured
first.  Likewise, Firefox JIT support is already reproducibly integrated in `origin/main`,
but a root image without the overlay marker intentionally falls back to Firefox ESR.

## Scope

This milestone owns:

- deterministic offline input-to-paint and local-navigation probes;
- browser, Xorg, framebuffer, and network timing evidence with bounded output;
- rootfs provenance for Xorg, the display provider, and the Firefox JIT overlay;
- fixes to confirmed fbdev, browser-launch, scheduling, or network hot paths;
- stable QEMU gates followed by bounded physical qualification;
- a display-provider contract that keeps the current fbdev path as the fallback.

This milestone does not own:

- DRM/KMS kernel implementation or `kernel/src/device/drm/`;
- GPU acceleration, virgl, native scanout, fences, or modesetting;
- speculative RISC-V framebuffer cache-policy changes without a hardware contract;
- repeated rootfs transfer or destructive board provisioning on every experiment.

## Considered approaches

### Directly change framebuffer caching

Mapping the framebuffer as write-combining could be valuable, but cache semantics on
RISC-V depend on PBMT and the EIC7700 memory/coherency contract.  Doing this before a
measured bandwidth result risks corruption and overlaps low-level work better reviewed
with hardware evidence.  It is not the first step.

### Wait for the DRM implementation

DRM is the long-term display architecture, but waiting would leave input, browser, and
network regressions unmeasured.  It would also make a large future merge responsible for
unrelated failures.  This approach is rejected.

### Measure and optimize the stable fallback

The selected approach adds low-volume probes to the current fbdev system, changes only a
confirmed bottleneck, and keeps all performance gates backend-neutral where possible.
It provides both an immediately usable system and a baseline for the DRM team.

## Architecture

### 1. Provenance contract

Every browser-performance result records:

- kernel commit and rootfs manifest digest;
- Xorg and fbdev package versions;
- display provider (`fbdev` now, `drm` later) and resolution/stride/bits per pixel;
- Firefox executable/version and whether the JIT overlay marker is present;
- network mode (`local`, `direct`, or `proxy`).

The gate fails closed when a required field is missing.  This prevents a result from an
old rootfs or a different Xorg binary being attributed to current code.

### 2. Deterministic interaction workload

Extend the existing physical-interaction HTML fixture with a machine-readable latency
API.  A synthetic input sequence records the browser event timestamp, schedules a
`requestAnimationFrame`, changes a visible paint token, and records the first following
frame.  A second link switches between two local pages served by the existing Megrez
network fixture.  The workload does not access the public Internet.

The browser gate reports individual samples and summary statistics in JSON.  It bounds
sample count and duration and rejects non-monotonic timestamps, missing paints, browser
PID changes, or an incomplete navigation.  Initial acceptance targets are:

- input-event-to-frame p95 at or below 100 ms;
- local navigation `DOMContentLoaded` p95 at or below 2 s;
- zero Firefox restarts during the workload.

Targets are admission criteria for the stable image, not claims about public websites.

### 3. Layered display evidence

The fixture measures browser-visible latency.  Host/guest evidence additionally records
Xorg CPU time, context switches, framebuffer geometry, and bytes attributable to the
bounded interaction interval.  If the current packaged fbdev stack exposes damage
statistics, use them.  Otherwise compare identical 1920x1080 and 1280x720 runs: a
latency change proportional to pixel count is evidence for scanout-copy bandwidth, not
proof by itself.

Only after provenance and the A/B result identify the display copy as dominant may the
implementation add a reproducible Xorg/fbdev source overlay.  Such an overlay must live
under `tools/riscv/debian/rootfs/`, be content-addressed in the rootfs manifest, update
only damaged rectangles, coalesce adjacent rectangles, and retain a bounded full-screen
fallback for unknown damage.  A source-tree-only patch under `target/` is forbidden.

### 4. Browser execution mode

Build and gate two immutable rootfs variants from the same base manifest: ESR fallback
and the existing frozen Firefox JIT overlay.  Run the deterministic local workload on
both before selecting the JIT image for physical deployment.  The launcher continues to
select the overlay only through its audited marker; no runtime package download is
allowed.

### 5. Network separation

After local interaction passes, reuse `megrez_network_fixture.py` for 1, 4, 8, and 16
bounded concurrent resources.  Record connect, first-byte, completion, and browser DOM
timings.  A local failure belongs to the kernel/socket/display/browser path.  A local
pass with a proxy/public-page failure belongs to proxy or external-network analysis.
This experiment must finish before changing TCP, `poll`, or scheduler code.

### 6. Display-provider boundary for DRM

The desktop session remains the single owner of Xorg.  Rootfs configuration chooses one
provider using an explicit value:

```text
ASTERINAS_DISPLAY_PROVIDER=fbdev|drm
```

`fbdev` remains the default and uses `/dev/fb0`.  A future DRM PR may add the `drm`
configuration and `/dev/dri/card0` readiness contract without changing the Firefox,
interaction, navigation, or evidence schemas.  Unknown providers fail before Xorg is
started.  This milestone will not add a fake DRM implementation or modify DRM kernel
files merely to create an extension point.

## Failure behavior

All probes have explicit deadlines and write a short terminal marker plus a structured
artifact.  Failure preserves the desktop when requested for demonstration, but the
automated gate terminates only the processes it started.  It never reboots the board,
rewrites partition 2, or replaces the root image unless a separately invoked deployment
step is requested.

## Test strategy

Host unit tests validate schemas, percentile calculation, timestamp ordering,
provenance rejection, display-provider selection, and timeout cleanup.  QEMU tests use
the persistent development container and prove both the fbdev fallback and local
interaction workload.  Physical tests are staged:

1. one read-only provenance capture on the current image;
2. one resolution A/B using the same kernel/rootfs when supported by firmware;
3. one candidate-image run after all host and QEMU gates pass;
4. three bounded interaction cycles in that single boot, followed by a stability window.

Multiple samples are collected in one boot; three manual board-reset rounds are not
required.  Raw samples, summaries, logs, and manifest identities remain together in one
evidence directory.

## Merge boundary

Performance commits remain focused on `tools/riscv/debian/rootfs/`, its tests, and only
those kernel subsystems proven to dominate a measured workload.  The branch must not
touch `kernel/src/device/drm/` or the DRM team's validation scripts.  Before integration,
the final diff is compared with the DRM branch and any shared rootfs configuration is
kept to the provider variable and stable evidence schema described above.
