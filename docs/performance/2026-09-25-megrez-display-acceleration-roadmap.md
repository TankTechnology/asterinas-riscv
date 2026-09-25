# Megrez display acceleration: research and staged development plan

This is a development plan, not a claim that native display, GPU rendering, or
video decode already works on Asterinas. It uses the current DRM/main candidate,
the controlled physical boot, the prepared Megrez device tree, and the RockOS
reference driver at `rockos-v6.6.y` commit
`bf2ec5d53002c16bc1bc593b92516eb6c2866176` (inspected on 2026-09-25).
Do not reboot the current desktop merely to repeat reference-system discovery.

## Hardware and software facts

- Milk-V lists an HDMI 2.0 output, an integrated GPU with OpenGL ES 3.2 and
  Vulkan 1.2 support, and H.264/H.265 codec capability on Megrez. These are
  **hardware capabilities**, not evidence of Asterinas driver support.
- The prepared Megrez DTB identifies `eswin,dc` at `0x502c0000`,
  `eswin,eswin-dw-hdmi` at `0x502a0000`, `img,gpu` at `0x51400000`, and
  `eswin,galcore_d0` at `0x50140000`. The relevant nodes are enabled and marked
  `dma-noncoherent` where applicable. The DTB is an integration input, not a
  substitute for checking the running board's register state and address map.
- The captured RockOS boot log shows `es_drm` binding the display controller
  and HDMI, and registering a DRM framebuffer. The RockOS source separates
  display KMS (`drivers/gpu/drm/eswin`) from the PowerVR render driver
  (`drivers/gpu/drm/img/img-volcanic`). `DRM_ESWIN` explicitly says it provides
  modesetting and buffer management, not 2D or 3D acceleration. RockOS's FAQ
  also identifies `pvrsrvkm`, a PowerVR Vulkan ICD, and a customized Mesa stack.
- Asterinas's physical DRM backend currently reads a GEM VMO row into scratch
  memory, writes that row to the firmware framebuffer, and synchronizes the
  destination. It does not program the controller, flip a hardware scanout
  address, handle vblank, or enable GPU commands. Its `MODE_PAGE_FLIP` currently
  calls the same synchronous present path and does not deliver a flip-complete
  event. Its `SET_CLIENT_CAP(ATOMIC)` succeeds while no atomic property/ioctl
  model is exposed; that must be made truthful before enabling atomic clients.
- The controlled 1920 x 1080 boot measured one 8.29 MB full present in 78.627 ms
  and five mostly idle dirty presents averaging 1.274 ms. A subsequent short
  Firefox/X11 operation added 101 dirty presents and 3.377 s of cumulative
  present time over a 17-second observation window. The URL and visible frames
  were not independently verified, so this is attribution evidence, not a
  measured user-input latency or speedup. See the P0 evidence note below.
- A separate selected boot with the read-only `asterinas.dc_probe=1` diagnostic
  verified the live primary scanout at `0xfd800000`, a 7680-byte stride, and a
  64 MiB contiguous GEM pool at `0xf8000000`. These ranges do not overlap and
  both fit a 32-bit register, but that does **not** establish DMA coherency or
  authorize pointing the controller at the current GEM pool. RockOS allocates
  display GEM memory with `DMA_ATTR_WRITE_COMBINE`; Asterinas currently maps
  its VMO-backed dumb-buffer pool with the normal write-back page policy. See
  the [handoff evidence](2026-09-25-megrez-dc-handoff.md).
- A later opt-in row-phase sample of a short Firefox/X11 operation attributed
  about 54% of sampled row time to GEM reads and 46% to framebuffer writes
  including synchronization. Xorg CPU time rose by 3.504 s while DRM dirty
  present time rose by 3.361 s. Optimizing only one side of the copy is
  unlikely to remove the dominant cost; a DMA-safe zero-copy path remains the
  P1 target. See the [phase evidence](2026-09-26-megrez-firefox-phase-profile.md).
- An opt-in GEM-page-to-firmware-framebuffer copy removes the scratch-row
  transfer while keeping the working firmware scanout. QEMU pixel tests and a
  selected physical desktop boot passed. A similar short window reduced
  dirty-present nanoseconds per copied byte by about 15%, with uncertain
  Firefox page state. This is a useful interim improvement, but not the 2×
  user-experience goal. See the [direct-copy evidence](2026-09-26-megrez-direct-copy.md).
- A selected physical boot cleaned one 8.29 MB GEM frame through the EIC7700
  cache operation in 6.746 ms, while leaving the display registers untouched.
  This supports an isolated direct-scanout gate, but does not yet prove an
  actual DMA read or a visible speedup. See the
  [DMA-clean evidence](2026-09-26-megrez-dma-clean-probe.md).

## Three implementation choices

1. **Improve firmware-framebuffer copying.** Lowest bring-up risk and useful
   fallback, but CPU still moves pixels and hardware modes/cursor remain absent.
2. **Adopt the existing firmware mode, then flip the display controller's
   primary scanout address.** Recommended first hardware milestone: narrowly
   eliminates the full-frame copy without claiming complete modesetting. It
   depends on a verified DMA address, cache-coherency and vblank contract.
3. **Port full native KMS and PowerVR rendering together.** Broadest result,
   but couples clock/reset/HDMI, memory management, vendor GPU UAPI and Mesa;
   failures would be hard to localize. Split it into later independent gates.

## Work packages and acceptance gates

### P0 — Attribute one real Firefox interaction and freeze the board contract

Use the existing root serial control channel and one 10–20 second natural
Firefox scroll/window interaction at 1920 x 1080. Capture start/end
`ASTERINAS_DRM_SCANOUT` counts, per-thread Firefox/Xorg `schedstat`, and a
small number of phase samples for VMO read, framebuffer write, and device
cache synchronization. Do not timestamp every row. Record the page, input
sequence, boot ID, kernel hash, and display mode. A one-window sample decides
whether the display copy contributes materially to the visible lag; it does
not by itself establish a speedup.

Separately, without taking over the current board, extract the RockOS source
contract for the DC register aperture, primary-plane format/stride/address,
shadow-register commit, vblank/underflow interrupt, HDMI route, clock/reset,
EDID, and buffer allocation. Compare it with the exact selected DTB and the
RockOS boot log. The gate is a reviewed register-and-DMA inventory, including
whether the current bootloader mode can be safely adopted and whether the
GEM pool's physical address is in the DC's reachable DMA window.

### P1 — Fixed-mode native display scanout

Add a safe-Rust EIC7700 display backend behind `ScanoutBackend` in
`kernel/src/device/dri/`, selected only when the compatible DT node and all
required resources validate. Preserve the firmware backend as the fallback.
Keep any unavoidable raw MMIO mapping or low-level synchronization in OSTD;
do not put `unsafe` in `kernel/`. Use the current physical mode initially, with
one XRGB8888/BGRX8888 primary plane and two or three pinned, DMA-reachable
buffers. Constrain pitch and address alignment to the vendor driver's checked
values; the existing GEM pool is physically contiguous, but that alone does
not prove the controller can DMA from every allocation. If its address is not
reachable, allocate a display-specific DMA-safe pool through OSTD and expose
that pool through the same GEM/mmap contract; never truncate or mask a physical
address until a test happens to show pixels.

Before handing a buffer to the controller, synchronize CPU writes to a
`dma-noncoherent` display engine. Change only the plane base address at a
verified safe boundary; retain the previous buffer until a real vblank/flip
completion. Deliver `DRM_EVENT_FLIP_COMPLETE` only after completion, reject a
second in-flight flip with `EBUSY`, and handle close/error paths without
releasing a displayed buffer. Do not infer that a successful synchronous
`MODE_PAGE_FLIP` already meets this contract. Initial mode changes, HDMI
hotplug and GPU rendering remain out of this milestone.

Audit Xorg's actual `SETCRTC`/`DIRTYFB`/`PAGE_FLIP` sequence before choosing the
initial buffer strategy. If Xorg repaints one mapped front buffer, direct
scanout removes the copy but can tear; require a verified back-buffer flip
path for the tear-free acceptance claim. Do not manufacture flip-complete
events or claim double buffering when userspace never requested it.

QEMU tests cover buffer lifetime, address and pitch rejection, flip sequencing,
vblank event order, underflow accounting, and fallback selection using fake
MMIO/interrupts. QEMU cannot prove EIC7700 register behavior. The physical gate
uses one reversible selected boot with nonce-framed UID 0 and boot-ID checks
before and after reopening the serial port, plus an ordinary RockOS recovery
path. A bounded display test checks correct frames, no stale/torn image or
underflow, and measures presentation CPU time and interaction latency. Do not
claim a user-visible improvement until the same short operation is compared
against P0 under the same mode and workload.

### P2 — Native mode setting and desktop integration

Only after fixed-mode flips are stable, add connector/EDID mode enumeration,
mode validation, HDMI link setup, clock/reset ownership, hotplug, blanking and
hardware cursor. The repo has board-specific clock programming patterns but no
general display clock/reset framework; introduce the smallest safe hardware
abstraction needed, rather than scattering magic register writes in DRM ioctl
handlers. Extend the KMS object and event model only to match supported UAPI;
do not advertise atomic mode setting until atomic commits/properties work.

Gate with an HDMI mode chosen from the monitor's EDID, the existing 1920 x
1080 mode, cursor movement, unplug/replug, and reboot persistence. Keep the
fixed firmware path recoverable if EDID or clock setup fails.

### P3 — GPU rendering, a separate render driver

Inventory the **exact** RockOS GPU kernel/UAPI and user-space package versions
used on this board, including `pvrsrvkm`, the PowerVR ICD, Mesa/GBM/EGL, and
the Xorg configuration. Then decide whether a compatible, maintainable GPU
render-node implementation is feasible in Asterinas. The `es_drm` display
driver is not that implementation. GPU work needs its own buffer allocation,
command submission, isolation, fences, cache synchronization, recovery, and
cross-device dma-buf import/export contract. Existing Asterinas PRIME handles
currently import buffers from the same device only, so cross-device sharing
must be designed and tested before claiming zero-copy GPU-to-display.

The first GPU gate is `eglinfo`/GBM and a simple accelerated pixel probe with
the renderer identified as hardware, followed by Xorg glamor (or an
equivalent compositor), then Firefox compositor/WebGL. Test Firefox's own
graphics status and one real scroll workload; keep software rendering as an
explicit fallback. RockOS documents OpenGL via Zink on this GPU; verify the
specific GL/EGL path rather than inferring it from Vulkan alone. The current
Firefox launcher sets `MOZ_AVOID_OPENGL_ALTOGETHER=1`; remove that override
only for a controlled GPU gate, then keep it off only if the renderer and
interaction evidence show success. If RockOS's UAPI is coupled to an unavailable or
incompatible user-space driver, record that boundary and keep P1/P2 useful
without promising an immediate GPU port.

### P4 — Video decode and optional 2D engine

Treat `es_vdec`/`es_venc` and `eswin,galcore_d0` as separate devices. A video
path needs a codec userspace API, decode buffers, synchronization, and transfer
or plane import into the display path. Verify a short local H.264 clip in a
simple player before attempting Firefox playback. The 2D engine is only worth
integrating if P0/P1 telemetry shows composition or blit work remains dominant
and its UAPI is available. Neither subsystem is a prerequisite for P1.

## Success criteria and decision rules

- **P1 mechanism:** A full-frame update no longer performs an 8.29 MB CPU copy
  into the firmware framebuffer. DMA synchronization, flip submission, and
  vblank delay are reported separately; no display corruption or lost console.
- **User experience:** On the same 1920 x 1080 local Firefox fixture, report
  p50/p95 trusted-input-to-visible-frame latency and CPU time for Firefox,
  Xorg, and the kernel. The project goal is at least 2x improvement in the
  relevant p95 interaction metric, but no stage is declared successful solely
  because a microbenchmark improved. If P1 removes the copy yet Firefox
  remains CPU-bound, move effort to P3 or browser/kernel CPU hotspots.
- **Scope/cost:** One short, information-rich physical run per candidate;
  focused unit/QEMU gates before each physical boot. Avoid long load tests and
  broad repeated A/B sweeps. Preserve the debug-console and RockOS recovery
  workflow described in `tools/riscv/README.md`.

## References

- [Milk-V Megrez hardware overview](https://milkv.io/docs/megrez/overview)
- [RockOS FAQ: integrated GPU, `pvrsrvkm`, and Mesa](https://docs.rockos.dev/en/docs/faq/)
- [RockOS EIC7700 display Kconfig](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/eswin/Kconfig)
- [RockOS DC driver and vblank/plane update path](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/eswin/es_dc.c)
- [RockOS GEM/DMA handling](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/eswin/es_gem.c)
- [RockOS PowerVR Kconfig](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/Kconfig)
- [Linux DRM KMS and vblank documentation](https://docs.kernel.org/gpu/drm-kms.html)
- [Linux DMA mapping and cache synchronization documentation](https://docs.kernel.org/core-api/dma-api-howto.html)
- [Asterinas current physical boot evidence](2026-09-25-drm-main-physical-boot.md)
- [P0 Firefox/Xorg/scanout observation](2026-09-25-megrez-firefox-display-p0.md)
- [Live EIC7700 display handoff and DMA blocker](2026-09-25-megrez-dc-handoff.md)
- [P0 row-phase profile of a short Firefox operation](2026-09-26-megrez-firefox-phase-profile.md)
- [Opt-in direct GEM-page copy and physical result](2026-09-26-megrez-direct-copy.md)
- [Opt-in GEM cache-clean probe and physical result](2026-09-26-megrez-dma-clean-probe.md)
