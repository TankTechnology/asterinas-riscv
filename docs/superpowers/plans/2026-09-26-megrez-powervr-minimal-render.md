# Megrez PowerVR Minimal Render Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Draw and read back a 16 × 16 image with the Megrez PowerVR GPU under a selected Asterinas boot, then make the same render device usable by the desktop and Firefox.

**Architecture:** Keep the existing EIC7700 display controller driver separate from the PowerVR render device. Reuse the matching RockOS firmware and proprietary EGL/GLES client libraries, but first implement the missing vendor server contract. A trusted root-only GPU service is the preferred place for the vendor bridge logic; Asterinas must still own power, DMA allocation and cache synchronization, interrupt delivery, and the render-device lifetime. Do not expose a PowerVR render node until a hardware pixel test succeeds.

**Tech Stack:** Asterinas safe-Rust kernel, OSTD DMA/MMIO facilities, EIC7700/RGX hardware, RockOS DDK 24.2@6643903, DRM vendor bridge ABI, Debian EGL/GBM/GLES.

---

## Evidence and constraints

- A selected Asterinas boot already reads `RGX_CR_CORE_ID = 0x001e000301980065` (BVNC 30.3.408.101) after enabling the board's three GPU clocks and five reset bits, then restores them. This establishes identity and the power sequence, **not** rendering. See [physical evidence](../../porting/evidence/2026-09-26-megrez-gpu-powered-id/README.md).
- The matching RockOS kernel, firmware, and vendor libraries rendered the [16 × 16 GLES pixel test](../../../tools/riscv/drm/gles-pixel-probe.py) on this GPU. Its trace has 188 `PVR_SRVKM_CMD` calls over 26 bridge functions, including GPU memory allocation/mapping and TA/3D and transfer submissions. See [reference inventory](../../performance/2026-09-26-megrez-gpu-readiness-check.md).
- The RockOS driver implements `PVR_SRVKM_CMD` through `PVRSRV_BridgeDispatchKM`; the tested client does not use the upstream PowerVR `CREATE_BO`/`SUBMIT_JOBS` ABI. Merely installing EGL/Mesa packages or copying the RockOS Linux kernel module cannot make the Asterinas render node implement that bridge.
- The [26 observed commands](../../performance/2026-09-26-megrez-powervr-bridge-functions.md) are now mapped to the pinned vendor headers. Memory operations account for 160 of 188 calls; GPU firmware and MMU initialization happen before any of these bridge dispatches.
- The GPU DT node has `dma-noncoherent` and no `iommus` property. The current Asterinas RISC-V IOMMU is not configured. A root service that controls GPU page tables and MMIO therefore has kernel-level hardware privilege; it cannot be treated as an isolated browser process. Firefox must never receive raw MMIO or physical DMA addresses.
- Asterinas's current `/dev/dri/renderD128` belongs to its display DRM implementation. It must not be advertised or tested as PowerVR. The current Firefox wrapper deliberately selects software rendering. Keep that default until the hardware pixel gate passes.

## File and ownership map

| File or component | Responsibility |
| --- | --- |
| `kernel/src/device/dri/powervr_probe/` | Existing DT/CRG validation and reversible power sequence; reuse the validated sequence without acquiring the same MMIO range twice. |
| `kernel/src/device/dri/powervr/` | New PowerVR device ownership, firmware boot, DMA allocations, interrupts, and bounded service transport. Keep it separate from `dri.rs` display/KMS ioctls. |
| `ostd/src/mm/dma/` | Existing `DmaCoherent` and `DmaStream` primitives. Add architecture-specific code here only if the existing noncoherent contract fails on physical hardware. |
| `kernel/src/device/dri/prime.rs` | Existing display GEM/PRIME handling; extend for GPU-to-display buffer sharing only after headless render works. |
| `tools/riscv/drm/gles-pixel-probe.py` | Existing independent hardware pixel acceptance test. |
| `tools/riscv/debian/rootfs/browser_m5_firefox.sh` | Remove the software-only environment gate only for a selected image that passed GPU and display gates. |

## Task 1: Pin the reusable software contract

- [x] Record SHA-256 and package versions for `rgx.fw.30.3.408.101`, `rgx.sh.30.3.408.101`, `libVK_IMG.so`, and the RockOS EGL/GLES/GBM libraries from the working reference boot. The [manifest and local staging instructions](../../porting/evidence/2026-09-26-megrez-powervr-task1-rockos-lifecycle/README.md) keep licensed binaries out of Git.
- [x] Decode the observed 26 bridge function IDs against RockOS commit `bf2ec5d53002c16bc1bc593b92516eb6c2866176`, including input/output sizes and the returned bridge status. The matching RockOS kernel passed the 16 × 16 pixel test with all 188 inner bridge statuses zero. See [reference evidence](../../porting/evidence/2026-09-26-megrez-powervr-bridge-status/README.md).
- [x] Record firmware loading and the driver/hardware shutdown boundary in the [same bounded RockOS trace](../../porting/evidence/2026-09-26-megrez-powervr-task1-rockos-lifecycle/README.md). The driver does not print a separate firmware shutdown acknowledgement; clean module removal, render-node disappearance, reset assertion, and clock disable establish the observable shutdown. The pinned DDK source places GPU MMU, firmware, and device teardown inside the kernel server, so a root daemon can only translate client requests after Asterinas owns those facilities. Do not implement 26 empty ioctl stubs.

## Task 2: Add a selected-boot GPU owner

- [x] Add an opt-in `asterinas.powervr=1` gate. Validate the exact Megrez DT resource shape, compatible string, BVNC, and expected powered-off CRG state before mutation. Preserve the default RockOS boot entry and the existing Asterinas software desktop boot.
- [x] Reuse the same owner-held CRG and GPU `IoMem` mappings for the complete session; do not reacquire released `IoMem` intervals. On error or final close, assert reset before gating clocks and log the actual restored CRG readback.
- [x] Expose a root-only, exclusive `/dev/powervr-control` endpoint for the trusted service. Check initial-namespace `CAP_SYS_RAWIO` at open, deny a second owner, and keep Firefox and ordinary desktop users off this endpoint. No unrestricted `/dev/mem` API is exposed.
- [x] Verify in QEMU that a non-Megrez DTB leaves the device absent, and use mocked register I/O to test power-up, failure unwinding, exclusive ownership, and exact restoration. The selected physical boot used nonce-framed UID 0 and boot ID over the stable serial port, then closed and reopened it to confirm control. No firmware was started. See [Task 2 evidence](../../porting/evidence/2026-09-26-megrez-powervr-owner/README.md).

The owner holds CRG and GPU MMIO mappings behind one lock because OSTD does
not recycle acquired `IoMem` ranges. The selected boot retains the existing
software desktop profile. The control endpoint only manages the power lease;
firmware, DMA, GPU MMU, command submission, and rendering remain in Tasks 3–5.

## Task 3: Establish DMA and firmware startup

The [physical DMA preflight](../../porting/evidence/2026-09-26-megrez-powervr-dma-preflight/README.md)
allocated one pinned page and proved the CPU-side uncached alias, address
bounds, and RockOS-compatible identity address on the board. The two matching
firmware files are staged in the selected root image and verified by SHA-256.
GPU-side visibility, GPU MMU ownership, firmware execution and handshake remain
open; none of the Task 3 acceptance items below is complete. The same board
session exposed ext2 metadata corruption in the Firefox profile. A complete
partition backup preceded offline repair, and a read-only fsck passed after a
subsequent clean desktop boot. This is a stability gate for further writable
GPU experiments, not evidence that the ext2 root cause is fixed.

- [ ] Allocate pinned, zeroed GPU memory through `DmaCoherent`/`DmaStream` with checked size limits and ownership tied to the GPU session. Establish and test the board's actual device-address range; do not assume CPU virtual addresses are GPU addresses.
- [ ] For the DT-declared noncoherent GPU, demonstrate a correct CPU/device visibility path using RISC-V cache synchronization or an uncached alias. Reject the selected boot if neither is available. Map only owned buffers into the GPU MMU and require the GPU page tables to reference those allocations.
- [ ] Load the exact BVNC-matched firmware and shader blobs from the selected root image. Report firmware handshake and GPU fault/timeout counters. Bound every wait and ensure reset/cleanup on failed handshake.
- [ ] QEMU-test allocation limits, buffer lifetime, invalid address/offset rejection, and failed firmware cleanup. On board, first require firmware ready and a clean shutdown/reboot with the root serial control path intact.

## Task 4: Reuse the vendor bridge for a headless pixel

- [ ] Implement the observed bridge initialization, memory, sync, TA/3D, and transfer operations with checked sizes and per-session handles. Prefer a trusted userspace service where it reduces porting; keep kernel-owned DMA, power, IRQ, and fault recovery. Preserve the vendor client's ioctl numbers and struct layout. Reject unsupported bridge functions explicitly.
- [ ] Register a distinct PowerVR DRM render node only when firmware and bridge initialization are live. Its identity and sysfs sibling list must be separate from the EIC7700 display card.
- [ ] Run the unchanged [16 × 16 GLES probe](../../../tools/riscv/drm/gles-pixel-probe.py) against that render node. Require the PowerVR renderer, left white/right black pixel values, zero bridge status, no GPU fault, and a clean second run. Record the Image, DTB, firmware, and library hashes in the result.
- [ ] Retain the prior selected-boot recovery discipline: original RockOS default, fresh UID-0 serial responses, and a separate post-reopen control check. Do not call a render node or Firefox GPU acceleration ready on module enumeration alone.

## Task 5: Connect GPU rendering to the desktop

- [ ] Add cross-device dma-buf/PRIME import or an explicit bounded copy from PowerVR output to an EIC7700 GEM buffer, with ownership, cache synchronization, and fences. Verify a GPU-produced test pattern through the display controller's scanout/readback path before relying on visual inspection.
- [ ] Configure EGL/GBM for the separate render node and display card. Only then try accelerated Xorg/Firefox in a selected profile; remove `MOZ_AVOID_OPENGL_ALTOGETHER=1` only there.
- [ ] Compare hardware and software profiles with the same bounded page/animation workload, collecting Firefox renderer choice, frame timing, CPU time, GPU faults, and serial liveness. Keep the software profile as recovery until both pixel correctness and stability pass.

## Stop conditions

Stop a selected experiment and restore the known-good desktop/RockOS path if CRG readback drifts, firmware handshake times out, a GPU fault appears, DMA visibility cannot be proved, the serial control path is lost, or the pixel result differs. The absence of HDMI capture limits monitor-level claims; internal GPU readback and display-controller readback can prove their respective stages only.
