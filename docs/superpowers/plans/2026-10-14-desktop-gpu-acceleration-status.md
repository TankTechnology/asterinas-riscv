# Desktop GPU acceleration status

Updated: 2026-10-17 (Asia/Shanghai)

This file is the status source of truth for the six phase plan in
[`2026-10-14-desktop-gpu-acceleration.md`](2026-10-14-desktop-gpu-acceleration.md).
The checkboxes in the implementation plan describe the intended work; this
board records what has actually been demonstrated by a current runtime receipt.
Host fixtures, parser tests, and source inspection are listed separately and do
not substitute for a QEMU or physical receipt.

## Completion layers

1. **QEMU acceleration layer:** Tasks 2–5 must have current guest receipts,
   including `renderer=virgl`, deterministic pixels, and recovery/fault gates.
2. **Application layer:** Task 6 must show Firefox WebRender and virgl on the
   same QEMU DRM desktop with a fixed local scene.
3. **Physical layer:** Task 7 is a separate board qualification. QEMU virgl
   never counts as EIC7700 PowerVR acceleration.

## Stage board

| Stage | Acceptance standard | Runtime evidence | Status | Next gate |
| --- | --- | --- | --- | --- |
| Task 1 — provider boundary | QEMU 2D DRM scanout, ordered SETCRTC → transfer → flush, pixels, and recovery | `docs/porting/evidence/2026-10-15-qemu-drm-scanout-current.md`; `docs/porting/evidence/2026-10-16-qemu-drm-scanout-gtk.md`; `docs/porting/evidence/2026-10-17-qemu-fbdev-vnc-scanout-r2.md`; `docs/porting/evidence/2026-10-17-qemu-fbdev-recovery-r1.md` | **complete (QEMU 2D scanout + fbdev recovery)** | Preserve fbdev as fallback while Task 6 is qualified |
| Task 2 — render node/GEM | card/render permission split; GEM map/name/open/close; PRIME; bounded ENOMEM; two-file lifetime | `docs/porting/evidence/2026-10-15-qemu-drm-render-node-current.md`; `docs/porting/evidence/2026-10-16-qemu-drm-gem-prime-enomem.md`; `kernel/src/device/dri.rs` and `kernel/src/device/dri/prime.rs` lifetime ktests. The original `target/qemu-uboot` runtime directory is root-only; the readable Markdown receipt is the current review artifact. | **complete (QEMU runtime + ktests; raw run needs re-archiving for independent replay)** | Keep the readable GEM/PRIME receipt immutable while Task 3/5 builds on it |
| Task 3 — virgl protocol | capset/context/resource/backing/submit/transfer/fence, including 992/993-byte command bounds; 2D flush remains a separate control-queue contract | `docs/porting/evidence/2026-10-15-qemu-virgl-current.md`; `docs/porting/evidence/2026-10-16-qemu-virgl-transfer-boundary.md`; `docs/porting/evidence/2026-10-16-qemu-virgl-fault-matrix.md`. The original GL runtime directories are root-only; the readable Markdown receipts are the current review artifacts. | **complete (QEMU happy path plus context/submit/fence fail-closed recovery; raw run needs re-archiving for independent replay)** | Keep the fault receipts immutable; do not invent a virgl 3D `RESOURCE_FLUSH` command |
| Task 4 — GL QEMU | matching QEMU with `egl-headless` and `virtio-gpu-gl-device`, version/config/Mesa receipt | `docs/porting/evidence/2026-10-16-qemu-gl-build-manifest.md`; `docs/porting/evidence/2026-10-14-qemu-virgl-host-preflight.md`; `docs/porting/evidence/2026-10-15-qemu-virgl-current.md` | **complete (host/QEMU; build manifest now inspectable)** | Use only the binary identity in the manifest for accelerated gates |
| Task 4.5 — Mesa/libdrm topology | canonical DRM class/dev links, parent transport metadata, `drmGetVersion`/`drmGetDevice2` | `docs/porting/evidence/2026-10-14-qemu-drm-sysfs-topology.md`; `docs/porting/evidence/2026-10-16-qemu-drm-desktop-r2.md`; root-owned raw runtime `target/desktop-m4-current-overlay-20261016-status/gate-gl-r2/` | **complete (QEMU r2 ordered receipt)** | Keep topology behind the virtio-gpu provider check while Task 6 is qualified |
| Task 5 — Xorg/Mesa accelerated desktop | modesetting + `kmsdev=/dev/dri/card0`, no fbdev dependency, glamor/virgl, EGL/GLES and X11 pixel hashes, injected failure recovery | `docs/porting/evidence/2026-10-16-qemu-drm-desktop-r2.md`; `docs/porting/evidence/2026-10-16-qemu-drm-scanout-gtk.md`; `docs/porting/evidence/2026-10-16-qemu-virgl-fault-matrix.md`; `docs/porting/evidence/2026-10-16-qemu-drm-scanout-fault-matrix.md`; `docs/porting/evidence/2026-10-17-qemu-drm-recovery-r2.md`; `docs/porting/evidence/2026-10-17-qemu-drm-vtfix-r24.md` | **complete (QEMU DRM/Xorg glamor, renderer=virgl, pixels, and recovery)** | Start Task 6 Firefox WebRender gate on the same DRM-only rootfs |
 | Task 6 — Firefox WebRender | `about:support` WebRender=available, virgl renderer, fixed local scene pixels/frame times/restart | `docs/porting/evidence/2026-10-14-qemu-firefox-drm-scene.md`; `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r16.md` (NewSession/SetWindowRect pass, scene execution timeout); `docs/porting/evidence/2026-10-16-qemu-firefox-marionette-diagnostic.md` (about:blank A/B stops at NewSession); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r17.md` (local scene start, Marionette socket ready, NewSession timeout); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r18.md` (cold NewSession and transport probe pass, scene readiness timeout); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r19.md` (500 ms poll, QEMU exited before terminal marker); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r20.md` (async scene command no response); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r21.md` (scene state undefined with stale HTML); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r22.md` (scene state undefined even with current HTML hashes); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r23.md` (script present, no state); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r24.md` (no state and no window error); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r25.md` (explicit `javascript.enabled=true`, same state boundary); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r26.md` (DOM marker and 32 samples, screenshot viewport mismatch); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r27.md` (fixed geometry, screenshot path permission); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r28.md` (tmpfs transition, ext2 screenshot EIO); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r29.md` and `r30.md` (screenshot succeeds, no WebRender log); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r31.md` (MOZ log zero bytes); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r32.md` (WebGL renderer empty); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r33.md` (WebGL context unavailable); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r34.md` (`webgl.force-enabled=true`, no effect); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r35.md` (`webgl.disabled=false`, no effect); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r36.md` (`MOZ_X11_EGL=1`, no effect); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r37.md` (bounded GL log capture); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r38.md` (filtered GL failure evidence); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r39.md` (render supplementary group, node access confirmed); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r40.md` (user EGL/virgl pixels pass, Firefox driver exhaustion); `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r41.md` (diagnostic-only stale-scene run); `docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r42.md` (current scene, Firefox WebGL driver exhaustion after glxtest/virgl pass); `docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r43.md` (forced EGL creates context but strict renderer is llvmpipe); `docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r44.md` (hardware feature override exposes raw virgl but sanitizer returns Generic Renderer and WebRender context fails); `docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r45.md` (GfxInfo renderer spoof does not change sanitizer or WebRender failure); `docs/porting/evidence/2026-10-17-firefox-egl-device-selection-audit.md` (source-level cause); `docs/porting/evidence/2026-10-17-firefox-source-provenance-gate.md` (four source hashes and two zero-fuzz patches pass); `docs/porting/evidence/2026-10-17-firefox-local-builder-arch-mismatch.md` (local amd64 candidate: native PASS, riscv64 target FAIL) | **in progress (no authenticated patched Firefox build; no valid WebRender receipt)** | Obtain the pinned native-riscv64 builder or an audited riscv64 sysroot, then require `renderer=virgl` before screenshot, frame, and restart receipts |
| Task 7 — physical board | independent board DRM/GPU driver, one-shot pixel test, management-chain and recovery receipt | `/mnt/mydisk/asterinas-physical-basic-20261017-r2/result.json` (minimal Basic + RockOS recovery only) | **partial (management chain + minimal Basic pass; no display/DRM/GPU claim)** | Keep board in RockOS; schedule display-safe fb0 only after the QEMU/Firefox critical path permits it |

## Audit ledger for Tasks 2–4.5

This ledger is the per-check reconciliation used for the current board. A
`PASS` below means the cited runtime receipt (and, where stated, the focused
ktest) exists; a parser or fixture-only result is not promoted to a runtime
claim.

| Task | Check | Current evidence | Result |
| --- | --- | --- | --- |
| 2 | card/render node exposure and permission split | `2026-10-15-qemu-drm-render-node-current.md` (`DRM_RENDER_ALLOWED/REFUSED/CARD`) | **PASS** |
| 2 | GEM create/map/name/open/close and two-file lifetime | `2026-10-16-qemu-drm-gem-prime-enomem.md` (`DRM_GEM_CREATE` through `DRM_GEM_TWO_FILE`) | **PASS** |
| 2 | PRIME export/import and stale-fd rejection | same receipt (`DRM_PRIME_EXPORT/IMPORT/STALE_REJECT`) | **PASS** |
| 2 | bounded allocation returns `ENOMEM` | same receipt (`DRM_GEM_ENOMEM allocations=15`) plus `dri.rs` ktest | **PASS** |
| 2 | final/non-final GEM release | `kernel/src/device/dri.rs` object-lifetime ktest and the two-file runtime sequence | **PASS**; no independent `prime.rs` ktest is claimed |
| 3 | capset/context/resource/backing/submit/fence | `2026-10-15-qemu-virgl-current.md` | **PASS** |
| 3 | transfer in both directions and 992/993-byte boundary | `2026-10-16-qemu-virgl-transfer-boundary.md` | **PASS** |
| 3 | context/submit/fence fail-closed recovery | `2026-10-16-qemu-virgl-fault-matrix.md` | **PASS** |
| 4 | GL-capable QEMU binary and device/backend pairing | `2026-10-14-qemu-virgl-host-preflight.md`, `2026-10-15-qemu-virgl-current.md` | **PASS**; builder/container binary only |
| 4.5 | canonical DRM class/dev links and parent transport topology | `2026-10-14-qemu-drm-sysfs-topology.md`, `2026-10-16-qemu-drm-desktop-r2.md` | **PASS** |
| 4.5 | `drmGetVersion`/`drmGetDevice2` and modesetting `kmsdev` | `2026-10-16-qemu-drm-desktop-r2.md` and its hashed raw serial | **PASS** |

The older sysfs document contains an intentionally failed `eglInitialize`
attempt; it is historical evidence and is superseded by the r2 runtime
receipt. The r2 raw directory is root-only, so its readable index records
hashes and extracted markers rather than copying the large raw files.

## Immediate blockers

| Blocker | State | Evidence/condition |
| --- | --- | --- |
| ① GL-capable QEMU selected before guest launch | **closed** | Host preflight and the `egl-headless`/`virtio-gpu-gl-device` argv are recorded in the Task 4 receipts |
| ② DRM-only rootfs must not require `/dev/fb0` | **closed** | r24 contains `DEBIAN_DESKTOP_M4_DRM_FBDEV dependency=none action=skipped`. |
| ③ Mesa/libdrm sysfs topology | **closed** | r24 contains `DRM_LIBDRM_DEVICE PASS`, virtio transport, canonical card/render nodes, and `driver=virtio_gpu`. |
| ④ modesetting `kmsdev` | **closed** | r24 contains the ordered `driver=modesetting kmsdev=/dev/dri/card0 fbdev=none` marker and Xorg ready marker. |
| ⑤ renderer must be virgl, never llvmpipe | **closed for current QEMU happy path and classifier** | Current EGL/X11 receipt says `renderer='virgl'`; the classifier and host regression test explicitly reject `llvmpipe`/`softpipe`/`swrast`/`swgl` |

The host-side GL-QEMU preflight was independently rerun on 2026-10-17 in
the pinned local `asterinas/qemu-gl-env:20261014` image. It passed for QEMU
10.2.1 (`egl-headless=true`, `virtio-gpu-gl-device=true`); the reproducible
receipt is `docs/porting/evidence/2026-10-17-qemu-virgl-host-preflight-current.md`.
This only reopens a usable host launch prerequisite; it does not count as a
new guest runtime or Firefox WebRender result.

The first fresh guest runtime leg after that preflight is recorded in
`docs/porting/evidence/2026-10-17-qemu-virgl-param-runtime-r2.md`. The
isolated QEMU run passed the virgl parameter contract (`reported_3d=1`,
`reported_capsets=2`, resource/backing/submit/fence completion) with no
network and read-only inputs. This is a Task 3 happy-path receipt only; the
next critical-path gate remains the strict DRM/Mesa/Xorg/Firefox chain.

The next isolated DRM scanout run is recorded in
`docs/porting/evidence/2026-10-17-qemu-drm-scanout-runtime-r2.md`. It passed
the ordered device/resource/SETCRTC/transfer/flush/readback sequence and the
1280×800 screenshot audit (`trace_count=5`, three-color lower bound). This is
the DRM scanout layer only; Xorg glamor and Firefox WebRender remain separate
gates.

A bounded submit-fault replay was attempted after the scanout pass. It is
explicitly **not passed**: the available boot disk contains the ordinary
`35edfd…` kernel, while the preserved fault manifest requires `b4888f…`; the
guest therefore followed the normal success path instead of emitting the
fault marker. The exact evidence is
`docs/porting/evidence/2026-10-17-qemu-virgl-submit-fault-replay.md`.
The next fault-gate attempt requires the matching fault kernel/initramfs and
boot disk to be re-archived; no automatic retry is justified with mismatched
inputs.

The current QEMU claim is therefore **virgl accelerated prototype with
separate happy-path and fail-closed recovery receipts**. It is not a Firefox
WebRender result and is not native EIC7700 GPU acceleration. The original
Task 2/3 runtime directories and the Task 5 r2 runtime directory are
root-only in the shared target tree; the readable Markdown receipts record
the exact raw-file hashes and extracted markers. A future rerun must archive
the raw serial/result files with the receipt before claiming independent
replayability.

## This round

The bounded implementation work in this round was (a) making the DRM desktop
classifier fail closed on software-renderer strings, (b) making the DRM rootfs
contract explicit about skipping `/dev/fb0` and pinning `kmsdev`, (c)
correcting final GEM/virtio resource release paths, including dma-buf close,
(d) running the readable v2 GL QEMU desktop gate, (e) closing the
Task 2 runtime object-space gate with independent card files, PRIME
export/import, stale-fd rejection, and bounded ENOMEM, and (f) registering a
GTK host scanout backend and running the same guest DRM probe through GTK/Xvfb
with a QMP screenshot and pixel hash, and (g) closing the independent 2D
SETCRTC/transfer/flush fault matrix, and (h) correcting the virgl self-test's
ordinary submit to use the complete ABI-valid NOP stream instead of a
one-byte request. The host tests, RISC-V kernel build,
filtered QEMU ktest, v2 DRM desktop happy path, GEM runtime receipt, GTK
scanout receipt, virgl 3D fault matrix, all three 2D fault legs, and the full
20-test virgl parameter suite pass. The QEMU r2 runtime receipt then closed
the ordered DRM-only and `kmsdev` markers that were missing from the older
v2 transcript. The r18 Firefox run also added a bounded Marionette transport
probe and a host-parser path that strips systemd serial prefixes before
checking the compact acceptance records; its scene timeout remains a Task 6
failure, not an acceleration pass. The current gate additionally emits one
redacted `FIREFOX_DRM_SCENE_STATE` record (scene URL/ready state/object type/
done/sample count) so the next run can distinguish navigation from timer
progress without logging page content. r25 repeated the same receipt with an
explicit `javascript.enabled=true` profile preference; that preference did not
change the missing page-state boundary. The r26–r33 runs then closed the
execution, geometry, and screenshot-write ambiguity: the DOM scene completes
32 samples and screenshots can be written to tmpfs, but r33 proves the
Firefox WebGL context is unavailable (`webgl_context=0`, `webgl_debug=0`).
No physical experiment was run. Task 6 is in progress and remains fail
closed; no WebRender, virgl, pixel, frame, or restart claim is made. r34 and
r35 then tested `webgl.force-enabled=true` and `webgl.disabled=false` one at a
time; neither changed the null WebGL context. r36 then tested the X11 EGL
environment (`MOZ_X11_EGL=1`) with the same result. r37/r38 added bounded GL
log capture and exposed `FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS`; r39 proved
that adding the `render` group grants read/write access to the render node; r40
proved the same user can render virgl pixels through GBM/EGL while Firefox
still rejects its own WebGL driver options. r41 and r42 then showed that the
same Firefox bundle's bounded `glxtest` also reports `RENDERER virgl` and
`TEST_TYPE EGL`; r42's current scene still reports
`FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS` with `webgl_context=0`. r43 then
tested the source-confirmed pair `webgl.force-enabled=true` and
`MOZ_WEBGL_FORCE_EGL=1`: Firefox created a context, but the page reported
`llvmpipe`, while the independent GBM probe and `glxtest` still reported
`virgl`. The strict renderer gate rejected the run before WebRender. The next
gate is therefore Firefox's own EGL display selection, not another DRM
permission or host EGL probe. r44 then set the temporary
`gfx.blacklist.webgl-use-hardware=1` preference: Firefox's raw GL renderer was
`virgl`, but its sanitizer returned `Generic Renderer`, and WebRender failed to
create a GL context. The strict gate remained closed. r45 then added
`MOZ_GFX_SPOOF_GL_RENDERER=VideoCore IV HW`; the raw `virgl` sanitizer error
and WebRender failure were unchanged, so this is not a GfxInfo-only blocklist
identity issue.

## Task 6 runtime audit

The r16 QEMU run is the current boundary receipt. It proves the same DRM
desktop reaches Xorg before Firefox, and that Firefox can create a Marionette
session and set its window rectangle. It then times out while reading the
fixed scene state after navigation, even after removing the synchronous
WebGL/Worker probes and using a bounded DOM/CSS timer scene. The next gate is
therefore a Firefox/Xorg execution investigation (page navigation and
`WebDriver:ExecuteScript` responsiveness), followed by the renderer log and
pixel/recovery records. No Task 6 success claim or physical experiment is
authorized by this receipt. The `about:blank` A/B diagnostic stops even
earlier at `WebDriver:NewSession`; that start URL is not a fix. The next
attempt keeps the r16 local-scene start URL and adds explicit window-context
selection before navigation and script execution. The gate now gives the
initial `NewSession` a separate 540-second bound inside the 600-second run and
enables payload-free Marionette transport diagnostics; the next runtime
receipt must show whether the request was sent and whether a response header
arrived.

A second bounded diagnostic is recorded in
`docs/porting/evidence/2026-10-16-qemu-firefox-marionette-diagnostic.md`. It
started from `about:blank`, connected to Marionette, and stopped at
`NewSession` before any page script ran. This is a separate fail-closed
diagnostic, not a WebRender result; it makes cold session establishment the
next Firefox investigation boundary.

The follow-up local-scene run is recorded in
`docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r17.md`. It fixes
the temporary image's executable mode and uses the r16 local scene start URL,
but still times out at `NewSession` after Marionette announces its port. This
contradicts neither the r16 scene-execution timeout nor the QEMU Task 5
receipt. The r18 follow-up uses a 540-second NewSession limit and reaches a
passing post-context `return 1` transport probe, then fails closed because the
local scene never reports ready before the 600-second run deadline. The
service budget is 1200 seconds for the scene/recovery pair. Task 6 remains
incomplete until the strict WebRender/virgl, pixel, frame, and recovery
records are present. r19 changes only the scene poll interval to 500 ms; it
reaches the same probe and scene boundary, but QEMU exits before a terminal
gate marker, so it is recorded as incomplete rather than as a timeout pass.
r20 replaces polling with one bounded async script; the command is sent after
the same transport probe but receives no response before the outer bound. r21
then exposed a stale scene artifact (`scene_type=undefined`): the current gate
was injected without the current HTML. The next run must inject and hash both
gate and scene sources before attributing any remaining stall to WebRender or
the DRM scene. r22 injected all three current sources and still observed
`scene_type=undefined`; r23 confirmed one 999-byte script and a working timer
API, while r24 added an error hook and observed no script error. r25 explicitly
set `javascript.enabled=true` and observed the same state. r26 then showed
`dom_marker=started dom_done=1 dom_samples=32`, proving the page script does
run; the missing `window` object is a cross-command sandbox observation issue.
The remaining boundary is now Firefox's GL context creation. r27 failed on the
guest screenshot path permissions and r28 on ext2 `EIO`; moving the disposable
PNG and diagnostic logs to `/tmp` made the screenshot stage reachable in r29
and r30. r31's MOZ log was exactly zero bytes, so it was not accepted as
renderer evidence. r32 added the WebGL debug-renderer probe and r33 recorded
the decisive state: the page script completed (`dom_marker=started`,
`dom_done=1`, `dom_samples=32`), but `getContext('webgl')` returned null and
no renderer was reported. The bounded r34 `webgl.force-enabled=true`, r35
`webgl.disabled=false`, and r36 `MOZ_X11_EGL=1` probes all produced the same
`webgl_context=0` result. r37/r38 added a bounded browser diagnostic and
recorded `FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS`; r39 added the missing
`render` supplementary group and recorded `read=1 write=1` for
`root:render 0660`; r40 then ran the same user-level GBM/EGL pixel probe and
obtained `renderer='virgl'` with the expected pixels before Firefox started.
Therefore the remaining boundary is Firefox's WebGL context creation after
both the user-level GBM/EGL probe and Firefox's bundled glxtest report virgl.
The r42 receipt is the bounded diagnostic closure for the pre-EGL probe path;
its strict failure remains `WebglAllowWindowsNativeGl:false` plus
`FEATURE_FAILURE_WEBGL_EXHAUSTED_DRIVERS`. The r43 receipt closes the null context boundary but proves the first failure:
Firefox's EGL/headless path selects `llvmpipe` even when GBM and `glxtest`
select `virgl`. The r44 receipt proves that enabling the hardware feature
reaches raw `virgl`, but the ESR sanitizer does not recognize that renderer and
WebRender then fails. The next gate must resolve that Firefox compatibility
boundary and make Firefox report a verified virgl-backed WebRender context; r45
shows that GfxInfo spoofing does not change the sanitizer. The strict
screenshot, frame, and restart requirements remain unchanged.

The r46 host-only step adds the normalized `renderer=...` value to the first
`FIREFOX_DRM_SCENE_STATE` marker, so the next runtime receipt will expose the
DOM renderer decision alongside the WebGL context flags. Its evidence is
`docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r46-marker.md`; the
21 focused host tests pass. This closes only the evidence-format gap. The
next runtime gate is still the Firefox WebRender EGL context boundary, followed
by a truthful `renderer=virgl` WebRender receipt.

The subsequent r46 QEMU runtime receipt is now archived at
`docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r46.md`. It records
`renderer=llvmpipe,_or_similar` with `webgl_context=1`, while the same guest's
GBM/EGL pixel probe and bundled `glxtest` report `virgl`. The gate therefore
fails closed before screenshot/frame/restart and the Task 6 status remains
incomplete; this is a Firefox renderer-selection boundary, not a physical
GPU result.

The r48 diagnostic is archived at
`docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r48.md`. Changing the
temporary DRM Xorg configuration to `DefaultDepth 32` caused Xorg to exit
before Firefox started, so the repository's `DefaultDepth 24` remains in
place. This candidate is rejected; the next implementation step is a
provenance-tracked Firefox source/build change and targeted EGL diagnostics.

The r47 QEMU diagnostic receipt is archived at
`docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r47.md`. A temporary
`gfx.egl.prefer-gles.enabled=true` preference did not change the boundary:
Firefox still sanitized raw `virgl` to `Generic Renderer` and returned a null
WebRender GL provider. The result is fail-closed before screenshot/frame/
restart; further progress requires a Firefox source/build change with explicit
package provenance.

The bounded source step is now closed: the provenance-tracked virgl sanitizer
patch and its focused regression test apply to the audited ESR source snapshot
with `patch --dry-run --fuzz=0` and a real zero-fuzz apply. The receipt is
`docs/porting/evidence/2026-10-17-firefox-virgl-source-patch.md`. This does not
count as Task 6 runtime completion; the next gate is an authenticated Firefox
source/package build followed by the existing QEMU WebRender scene gate.

The r50 QEMU runtime diagnostic is archived at
`docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r50.md`; it used an
older gate copy and is not treated as a current runtime receipt. The current
gate rerun is r51 at
`docs/porting/evidence/2026-10-17-qemu-firefox-drm-scene-r51.md`: the same
temporary `virpipe` environment caused the EGL probe to lose its rendering
server and Marionette to become unreachable. This closes the environment-only
diagnostic branch; Task 6 still requires a provenance-tracked Firefox
build/source change and a truthful `renderer=virgl` WebRender receipt.

The next source-level candidate is archived at
`docs/porting/evidence/2026-10-17-firefox-egl-render-node-source-patch.md`.
It makes the existing hardware WebRender request try the configured DRM render
node before the GTK default display, preserving fallback. The patch applies to
the audited Firefox source with zero fuzz; it is not a runtime pass until a
provenance-tracked Firefox build is produced and the QEMU scene gate proves
virgl, pixels, frames, and recovery.

The host-side source provenance gate is now closed. `source_patch_gate.py`
verifies the four pinned ESR source hashes and both compatibility patch hashes,
then checks ordered zero-fuzz applicability in a disposable tree; its 5-test
unit suite and the existing 21 Firefox gate tests pass. The receipt is
`docs/porting/evidence/2026-10-17-firefox-source-provenance-gate.md`. This is
not a Firefox build or runtime result. The next Task 6 gate is an authenticated
provenance-recorded Firefox package/source build containing both patches,
followed by the strict QEMU WebRender scene gate with `renderer=virgl`, pixels,
frames, and restart recovery.

The package-admission step is now implemented and tested. The new
`package_provenance_gate.py` validates a future patched `.deb`'s bytes and
Debian identity against the source manifest, both patch hashes, a source `.dsc`
hash, and a pinned builder-image digest without extracting or installing it.
Its synthetic-package test matrix passes, and the combined Firefox host suite
now reports 31 passing tests. The receipt is
`docs/porting/evidence/2026-10-17-firefox-package-provenance-gate.md`.
This closes only the admission-contract gate; no real patched package exists
yet. The next gate remains a reproducible ESR RISC-V build, followed by rootfs
integration and the strict QEMU WebRender receipt.

The exact Debian Security ESR 140 source descriptor is also anchored in
`tools/riscv/debian/rootfs/firefox_compat/source_package_manifest.json`; its
local descriptor comparison passed for the format, version, and two core
archive hashes/sizes. This is only an input anchor: the host has not verified
the OpenPGP signature or downloaded the 688 MB source archive. The next
external build prerequisite is a usable Debian archive keyring and fixed
RISC-V-capable builder image.

The build preflight is recorded at
`docs/porting/evidence/2026-10-17-firefox-build-preflight.md`: the actual
source tree is ready, but `dpkg-checkbuilddeps` reports missing GTK/DRM/Rust/
LLVM/Clang/cbindgen dependencies, the pinned builder image is absent, and only
about 6.4 GiB remains on the root filesystem. No build was started under that
budget. The new `build_firefox_esr.sh` driver now enforces those checks and
stops before Docker or source copying when the 12 GiB budget is not met. A
metadata-only registry probe also failed to obtain the pinned image: Docker
Hub timed out during TLS negotiation and configured mirrors returned 403/502/
404. No pull was attempted.

The development-root handoff is now implemented in
`tools/riscv/debian/rootfs/firefox_compat/compat_overlay.py`. It runs package
provenance first, validates archive traversal and symlink boundaries, checks
the Firefox runtime files, and writes a deterministic overlay marker without
touching dpkg metadata. Its failure matrix and the combined 34-test Firefox
host suite pass; the source package descriptor gate adds three more checks in
the current 37-test suite. The receipt is
`docs/porting/evidence/2026-10-17-firefox-compat-overlay.md`. This is still
not a real Firefox build or WebRender runtime result.

The source descriptor check is now executable via
`firefox_compat/source_package_gate.py`; it validates the pinned `.dsc` hash,
package/version/format, and both core `Checksums-Sha256` rows. The real local
descriptor passes. It deliberately does not claim OpenPGP signature
verification, so the build prerequisite remains a Debian keyring plus a fixed
RISC-V-capable builder. A disposable-keyring check has since verified the
descriptor's OpenPGP signature with signer fingerprint
`182E161D1130B9FCCD7DB167E42AA04FA6AA8C72`; this key is not installed into
the host trust database.

The real 4.1 GiB extracted ESR source tree now passes the zero-fuzz source
patch gate after both core archives were hash-verified. This closes the
source-layout uncertainty; the remaining build boundary is concrete:
`dpkg-checkbuilddeps` reports missing Firefox GTK/DRM/Rust/LLVM/clang/cbindgen
dependencies, and the pinned builder image is not present locally.

## 2026-10-17 bounded virgl fault step

The submit-fault recovery leg is now closed in QEMU. The minimal initramfs did
not mount `/proc`, so the first rebuilt fault image silently ignored the
selector and reached the normal virgl success marker. The fault profiles now
place `asterinas.virtio_gpu_fault={context,submit,fence}` after the kernel
`--` delimiter; the init probe parses the forwarded init argument and its
native self-test covers valid, invalid, duplicate, and prefix cases.

The focused Python/C suite passes (`23` tests). A fresh submit-fault run in the
GL-capable QEMU image reports exactly one `DRM_VIRGL_FAIL stage=submit` and one
`DRM_VIRGL_FAULT stage=submit`, then recovers through OpenSBI/U-Boot; its gate
result is `passed=true, recovery_complete=true`. The raw receipt and serial
hash are archived at
`docs/porting/evidence/2026-10-17-qemu-virgl-submit-fault-r1.md` and its
companion directory. This is a QEMU virgl fault/recovery result only; it does
not count as Firefox WebRender or physical-board evidence.

The context-init and fence fault legs are now also closed. Separate bounded
QEMU runs produced one `DRM_VIRGL_FAULT stage=context-init` and one
`DRM_VIRGL_FAULT stage=fence`, each followed by a complete OpenSBI/U-Boot
recovery (`passed=true`, `recovery_complete=true`). Their raw receipts and
serial hashes are archived at
`docs/porting/evidence/2026-10-17-qemu-virgl-context-fence-faults-r1.md` and
the two companion receipt directories. Task 3's three registered fault stages
(context, submit, fence) now have independent QEMU fail-closed evidence.

The next critical path remains Task 5/6: Xorg and Firefox must report a
truthful `virgl` renderer, then pass the fixed pixel/frame/restart contract.

On 2026-10-17 I attempted one bounded replay of the retained
`drm-desktop-rootfs-build` virgl bundle. The gate stopped before QEMU because
the boot disk payload identities do not match its `artifacts.json` manifest;
the exact hashes and command result are in
`docs/porting/evidence/2026-10-17-qemu-virgl-runtime-replay-audit.md`.
This is an input-integrity failure, not a new virgl result. The historical
Task 3/5 receipts remain valid as recorded, but a fresh replay needs a
matching archived boot disk and manifest; a read-only scan of all 130 retained
`boot.ext4` files found no match.

Task 5's host parser has also been hardened: `xorg_modesetting_gate.py` now
rejects missing or non-`virgl` `DEBIAN_DESKTOP_DRM_GL_XORG` markers. Its focused
five-test suite passes, and the older llvmpipe artifact is now correctly
classified as `passed=false`; the positive QEMU r2 `renderer=virgl` receipt is
unchanged. The parser-hardening receipt is
`docs/porting/evidence/2026-10-17-qemu-xorg-renderer-fail-closed.md`.

The runtime package identity gate is now also closed for the source-patched
Firefox path. `runtime_provenance_gate.py` compares the rootfs
`packages.lock` Firefox row with the exact package/version in the audited
source patch manifest. `target/dev-overlays/browser-web/rootfs` passes with
`140.16.0esr-1~deb13u1`; the current `target/debian-riscv/browser-web-drm`
rootfs is rejected because it contains `153.4.0esr-1~deb13u1`. The focused
three-test suite passes. The receipt is
`docs/porting/evidence/2026-10-17-firefox-runtime-package-identity-gate.md`.
This is an identity gate only: it does not prove that the runtime bytes carry
the patches or that WebRender is accelerated. The next Task 6 gate remains an
authenticated patched ESR RISC-V package build and strict QEMU WebRender
runtime evidence.

The strict Firefox scene parser now exposes a provenance-bound entry point,
`classify_scene_with_runtime`, and matching CLI options. It accepts a complete
WebRender/virgl scene only when the selected `packages.lock` also matches the
audited source package/version, and carries the lock SHA-256 in the result.
The focused scene plus runtime-identity suite passes (20 tests). A complete
synthetic transcript paired with the matching lock passes; the same transcript
paired with the current `browser-web-drm` lock fails closed on the 153.4 versus
140.16 version mismatch. The receipt is
`docs/porting/evidence/2026-10-17-firefox-scene-provenance-binding.md`.
These are host contract checks, not a QEMU runtime pass; the next gate is the
real patched ESR package build and provenance-bound QEMU WebRender run.

## 2026-10-17 bounded DRM desktop handoff gate

The Stage1 root-init parser now accepts the literal `--` argument delimiter
that U-Boot places before `--root-init=systemd`, while rejecting duplicate
delimiters. It also keeps the explicit `/dev/vdb` root selection fail-closed.
The native self-test covers the accepted delimiter, duplicate-delimiter
rejection, and fixed virtio/MMC root probes. The rebuilt initramfs hash is
`297d52d2d5eaf059bb559fc991ca15d134748066ba84f9de150930a3731ce3db`.

A bounded one-boot run in the GL-capable QEMU image then passed the DRM
desktop gate (`passed=true`, `reason=pass`). Its serial receipt proves Stage1
root handoff, `/dev/fb0` skipped, `kmsdev=/dev/dri/card0`, `virtio_gpu` sysfs
topology, Xorg `renderer=virgl`, the fixed white/black pixel probe, the
1280×800 X11 screenshot hash, and `DEBIAN_DESKTOP_M4_READY`. The receipt is
`docs/porting/evidence/2026-10-17-qemu-drm-stage1-delimiter-fix.md` and its
two raw files. This closes the minimal Stage1 handoff step and refreshes the
Task 5 QEMU runtime evidence; it is not Firefox WebRender or physical-board
evidence. The next critical-path gate remains a provenance-authenticated
patched Firefox build/runtime for Task 6.

The Task 6 rootfs build prerequisite also advanced: cache admission now reads
the archive identity before the authenticated hash lookup and skips only
superseded, no-longer-installed archives. Installed archives remain bound to
the signed index and fail closed on a missing or mismatched hash. The focused
tests, the 35-test builder class, and the full 214-test rootfs module pass;
the receipt is `docs/porting/evidence/2026-10-17-firefox-rootfs-cache-admission.md`.
The actual Firefox rootfs rebuild is still pending because the required
immutable builder image is unavailable locally (see the builder-availability
receipt). The build output filesystem now has about 42 GiB free on `/` and
2.5 TiB free on `/mnt/mydisk`; `/mnt/shared` remains effectively full and is
not used for new build output. This is a build prerequisite result, not
WebRender runtime evidence.

## 2026-10-17 bounded fbdev VNC scanout gate r2

The first VNC attempt used a Meson-default QEMU build and stalled in the
guest's `ldconfig.service`; an A/B run with that same build but no VNC stalled
at the same point. This isolated a QEMU configuration difference rather than
proving a VNC or guest filesystem defect. A new host-only QEMU binary was then
built with the original GL configuration (`--without-default-features`,
OpenGL, virglrenderer, pixman) and VNC as the only added feature.

The fresh one-boot scanout gate passed. The serial log reached
`DEBIAN_DESKTOP_M4_FB_SCANOUT_READY`, recorded 1280x1024/stride 5120 and
three framebuffer colors, and reached `DEBIAN_DESKTOP_M4_READY`. QMP
`screendump` produced a non-blank 1280x1024 PPM with pixel hash
`44b1d582f4d56d27b4c1715bfbc317f01dd2f8ed3c0c5742f2c2bda30f4b6b44`.
The machine-readable result is `passed=true`; raw artifacts and input hashes
are documented in
`docs/porting/evidence/2026-10-17-qemu-fbdev-vnc-scanout-r2.md`.
This closes only the one-boot fbdev scanout transport leg; recovery and
Firefox WebRender remain open.

## 2026-10-17 bounded fbdev recovery gate r1

The same minimal GL-capable QEMU configuration also passed the separate
recovery gate. The first desktop epoch reached `DEBIAN_DESKTOP_M4_READY`,
recorded an exact 16x16 framebuffer write/restore hash pair, and then the
bounded software reboot returned to a fresh OpenSBI/U-Boot banner and prompt.
The machine-readable result is `passed=true, reason=pass`; raw serial and
input hashes are documented in
`docs/porting/evidence/2026-10-17-qemu-fbdev-recovery-r1.md`.
This closes the QEMU fbdev boot/recovery half of Stage 1. The DRM recovery
leg, Firefox WebRender runtime, and physical-board validation remain open.

## 2026-10-17 bounded DRM recovery gate r2

The current DRM modesetting path now also has a fresh recovery receipt. The
gate uses one `asterinas.reboot_after=180` timer, reaches the DRM-only
`kmsdev=/dev/dri/card0` handoff, records `renderer=virgl`, the fixed pixel
probe, and the 1280x800 X11 screenshot hash, then returns to a new
OpenSBI/U-Boot epoch. The earlier r1 attempt exposed a duplicate 300/60-second
timer and stopped before the desktop marker; the boot-argument construction is
now unique and covered by tests. The passing result and serial hashes are
documented in `docs/porting/evidence/2026-10-17-qemu-drm-recovery-r2.md`.
This closes the QEMU fbdev+DRM provider boot/screenshot/recovery legs of
Stages 1 and 5. Firefox WebRender and physical-board validation remain open.

## 2026-10-17 bounded virgl runtime replay r3

A fresh identity-bound QEMU bundle was generated at
`target/qemu-uboot/virgl-replay-20261017/`; ext4 payloads were read back and
compared before launch. One GL-capable QEMU run reached the guest ready marker
and passed the base U-Boot handoff, but the strict virgl parameter classifier
returned `passed=false` with `missing or unordered transfer markers`. The
serial log contains 3D/capset/context/resource/submit/fence/backing/timing and
implicit-submission markers, but no transfer-to/from-host markers. The raw
receipt is `docs/porting/evidence/2026-10-17-qemu-virgl-param-runtime-r3/`.
This closes the input-integrity uncertainty for this fresh bundle; it does not
close the virgl parameter gate. The next single bounded attempt must use the
already retained transfer-boundary initramfs and regenerate a separate pair.

## 2026-10-17 virgl parameter runtime gate closed

The strict virgl parameter gate now passes in a fresh GL-capable QEMU run
using an independently generated bundle at
`target/qemu-uboot/virgl-replay-20261017-transfer/` and the retained
transfer-boundary initramfs. Payloads were read back from ext4 before launch.
The result reports `passed=true`, `3d=1`, `capsets=0x2`, valid BO/resource
handles, transfer-to/from-host boundary markers, submit, fence and backing
checks, followed by the ready marker. The raw receipt is
`docs/porting/evidence/2026-10-17-qemu-virgl-param-runtime-r3-transfer/`.
This closes the current Task 3 virgl parameter runtime replay gate in QEMU;
it is not Firefox WebRender or physical-board evidence.

## 2026-10-17 bounded fbdev VNC scanout attempt

One bounded host-scanout attempt used a freshly built QEMU 10.2.1 with VNC
support and the existing fbdev desktop profile. The gate failed closed before
the scanout marker: Stage1 reached `handoff-done action=exec`, then systemd
remained in `ldconfig.service/start running (1min 6s / no limit)`. No PPM was
published, so this is not a pixel pass. The machine-readable result records
`passed=false, reason=protocol`; the serial log and hashes are in
`docs/porting/evidence/2026-10-17-qemu-fbdev-vnc-scanout-r1.md` and its raw
directory. The next gate must first explain or remove this VNC-specific boot
stall; no real hardware test is scheduled from this result.

## 2026-10-17 status alignment

The stage table was reconciled with the newest runtime receipts. Task 1 now
points at the passing fbdev scanout and recovery records, and Task 5 points at
the passing DRM-only virgl pixel/recovery record. A path audit found no missing
evidence referenced by those two rows (`BOARD_EVIDENCE_MISSING []`). The audit
and focused regression tests are bookkeeping checks only; they do not create a
new runtime or physical-board receipt. Task 6 remains the next critical-path
gate because no authenticated patched Firefox WebRender run has passed yet.

## 2026-10-17 Firefox builder availability gate

The build-driver safety fix is covered by
`docs/porting/evidence/2026-10-17-firefox-build-driver-workdir.md`: the large
source copy is created under the checked `--output-dir` filesystem, and its
four regression tests pass. A fresh preflight with output on `/mnt/mydisk`
then passed the source-package and zero-fuzz patch gates but stopped before
compilation because the required immutable builder image is not local. The
complete result, including the missing dependency-qualified substitute-image
check, is recorded in
`docs/porting/evidence/2026-10-17-firefox-builder-availability.md`. This
advances the Task 6 entry gate without claiming a Firefox package or runtime
WebRender result.

## 2026-10-17 physical Basic management-chain gate

One bounded physical run used the already audited `f168…/7806…/465c…`
artifacts and a volatile minimal DTB profile. The profile disabled both GMAC
nodes, both DWC3 wrappers, and the firmware framebuffer, and removed the
`/chosen/asterinas,usb-host` selector. It reached the Asterinas boot-id and
`asterinas-basic#`, registered UART and MMC, reported the framebuffer absent,
and armed the 60-second software recovery timer. No GMAC, DWC3, framebuffer,
DRM, GPU, or network workload ran in Asterinas.

The bounded recovery reached a fresh OpenSBI/U-Boot epoch. One explicit
`run bootcmd` then selected the persistent RockOS entry. A fresh nonce-framed
SSH attestation recorded UID 1000, SSH active, and RockOS boot-id
`9c777a00-cc49-47c7-829a-c6abac19dbc9`. The machine-readable receipt and raw
serial/SSH logs are retained outside the full shared volume at
`/mnt/mydisk/asterinas-physical-basic-20261017-r2/result.json` and
`/mnt/mydisk/asterinas-physical-basic-20261017-r2/README.md`.

This closes only the physical management-chain and minimal Basic sub-gate;
Task 7 desktop/DRM/GPU validation remains open. The next critical path is
still the authenticated Firefox WebRender build/runtime gate in Task 6; any
physical display run must first pass that QEMU gate and use a separate,
recoverable display-safe cycle.

## 2026-10-17 local Firefox builder architecture audit

To make progress without silently pulling an unverified image, a local
candidate was built from the pinned local GL/QEMU image.  It passes the
Firefox Debian dependency check for its native `amd64` environment and has
the requested GTK/DRM/X11/audio/LLVM packages, cbindgen 0.27.0, and the
existing Rust 1.96 nightly toolchain.  It fails the actual target check:
`dpkg-checkbuilddeps -a riscv64 debian/control` reports the target GTK/DRM,
LLVM/Clang, wasm libc++, audio, and related development libraries absent.
The candidate has no foreign `riscv64` package architecture, no target
sysroot, and no installed `riscv64gc-unknown-linux-gnu` Rust standard
library.  It was therefore rejected as a Firefox builder; no `-d` bypass and
no compiler run were used.  Details, image digests, and the full log hash are
in `docs/porting/evidence/2026-10-17-firefox-local-builder-arch-mismatch.md`.

At the same checkpoint `/mnt/shared` had about 30.7 GiB free and
`/mnt/mydisk` about 2.5 TiB free.  The existing physical receipt remains in
RockOS after the minimal Basic/recovery gate; this audit did not touch the
board or start a display experiment.  Task 6 remains open until the pinned
native-riscv64 builder or a complete audited target sysroot is available.

## 2026-10-17 target-builder gate hardening and physical read-only check

The Firefox build driver now performs the target dependency check and a
`riscv64gc-unknown-linux-gnu` Rust metadata compile before any package build.
The five focused driver tests and `bash -n` pass.  A bounded replay against
the local amd64 candidate fails closed at the target dependency gate, with
`FIREFOX_ESR_BUILD FAIL reason=target dependency or Rust target check failed`;
no compiler or Firefox package was started.  The replay log is retained at
`/mnt/mydisk/firefox-builder-local-20261017/driver-gate-rerun-r2/run.log` and
the detailed evidence is
`docs/porting/evidence/2026-10-17-firefox-local-builder-arch-mismatch.md`.

The physical board was only read through the existing RockOS management path:
ICMP and SSH were reachable, the OpenSSH banner identified Debian, and an
authenticated read-only query returned UID 1000, boot-id
`9c777a00-cc49-47c7-829a-c6abac19dbc9`, `/dev/fb0`, `/dev/dri/card0`, and
`end1` up.  No serial write, reboot, display probe, or external-device access
was performed.  The read-only receipt is
`/mnt/mydisk/board-readonly-20261017/ssh-readonly.txt` (SHA-256
`432d1908dc164cc57346fce8b13814fac428200a99cba27ae44ffde25c9fd0c3`).

Storage remains above the requested reserve: `/mnt/shared` has about 30.7 GiB
free, `/` about 39 GiB, and `/mnt/mydisk` about 2.5 TiB.  No cleanup is
required to preserve a 30 GiB working reserve.  The next critical-path gate
is an authenticated native-riscv64 Firefox builder (or complete cross sysroot);
physical display work remains fail-closed until that QEMU/application gate is
closed.

## 2026-10-17 GL-QEMU backend, DTB, stage1, and storage replay

The bounded DRM recovery replay was repeated in the GL-capable QEMU image.
The ordinary host wrapper was rejected before launch because it has neither
`egl-headless` nor `virtio-gpu-gl-device`. The GL build was then used without
changing the guest artifacts. Its first generated four-hart DTB advertised
`sv57` and stopped the kernel after `Enter riscv_boot`; regenerating it with the
gate's CPU options produced a four-hart `sv39` DTB.

The resulting r8 receipt reached the kernel and correctly exercised the
fail-closed rootfs boundary, but the old stage1 image emitted
`DEBIAN_ROOTFS_FAIL reason=root-init-argument`. With the paired
`stage1-delimiter-20261017/initramfs.cpio`, r9 reached all explicit root
selection and handoff steps through `action=exec` and the Debian systemd
banner. It did not emit `DEBIAN_DESKTOP_M4_READY` or the strict virgl pixel
marker before the 180-second recovery boundary, so Task 5 remains open. The
full replay table and hashes are in
`docs/porting/evidence/2026-10-17-qemu-drm-host-and-stage1-r9.md` and the raw
receipts are under `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r6`
through `r9`.

The board remained in RockOS and was read only; the matching receipt is
`/mnt/mydisk/board-readonly-20261017/ssh-readonly.txt`. No physical display,
DRM, GPU, serial write, or reboot was performed.

The requested storage reserve was made explicit. The two old, reproducible
artifact collections removed from the inactive `gpu-fwif-current` worktree
were the 13 browser/virgl rootfs variants and its old `riscv64imac` target,
freeing about 38 GiB on `/`. The complete pre-delete file list and checksum
are in `/mnt/mydisk/storage-cleanup-20261017/manifest.txt` and
`manifest.sha256`. `/` now has about 77 GiB free, `/mnt/shared` about 30.7
GiB, and `/mnt/mydisk` about 2.5 TiB. Dirty worktrees, current artifacts,
Docker images, and evidence were retained.

Next QEMU gate: the existing `tools/riscv/drm/virgl_host_preflight.py`
already rejects a non-GL QEMU, but the desktop DRM launch path does not call
it before rootfs materialization. Wire that same preflight into the desktop
launch and add its fail-closed unit test, then isolate why the
`desktop-m4-x11` systemd target does not emit the desktop marker under the
corrected stage1 pairing. Do not start physical fb0/DRM until that gate
produces `renderer=virgl`, pixels, and the recovery receipt.

## 2026-10-17 DRM launch-boundary GL preflight (r10)

The desktop launch path now invokes a provider-specific QEMU argv validator
after constructing the complete command and before opening a PTY or calling
`launch_process()`. `DesktopM4DrmOperations` reuses
`virgl_host_preflight.inspect_qemu()` and fails closed when the selected
executable cannot answer `--version`, lacks `egl-headless`, or lacks
`virtio-gpu-gl-device`. The default hook is a no-op, so fbdev and existing
non-DRM gates keep their prior launch contract.

The new fail-closed test, the 23-test Desktop M4 regression class, the four
host-preflight tests, and Python syntax checks all pass. Evidence is in
`docs/porting/evidence/2026-10-17-qemu-drm-host-preflight-r10.md`.

The current host namespace still cannot execute the prior GL binary
(`/root/qemu-gl-src/build/qemu-system-riscv64`: permission denied), and no
default `qemu-system-riscv64` is installed. Thus r10 proves the launch gate,
not a new virgl runtime result. The next gate is to run the existing GL
container through this same path and obtain the strict desktop marker with
`renderer=virgl`, pixel evidence, and recovery. The physical board remains
read-only RockOS; no physical display experiment is authorized by the gate.

## 2026-10-17 GL-QEMU runtime and systemd boundary (r11–r13)

The GL container was rerun with the host render node mapped into the container.
This moved the failure boundary past host EGL initialization: r11 reached the
Debian root handoff and systemd, but reported `systemd-sysctl.service` and
`systemd-sysusers.service` failures and left `ldconfig.service` running until
the 180-second recovery deadline. No Xorg, `renderer=virgl`, pixel, or ready
marker appeared. The receipt is
`docs/porting/evidence/2026-10-17-qemu-drm-gl-runtime-r13.md` and raw output is
under `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r11`.

Two diagnostic replays were deliberately kept separate from the normal gate:
r12 placed temporary systemd masks after the stage1 delimiter and was rejected
by the root-init parser; r13 placed them before the delimiter, but
`systemd-sysctl.service` still started and remained pending. This rules out a
simple userspace mask workaround and points the next investigation at the
Asterinas proc/sysctl and early-systemd ABI. The normal DRM gate remains
fail-closed and Task 5 is still open; no physical display run is scheduled.

## 2026-10-17 desktop-m4 startup-cache experiment (r17)

The builder and cache checker now include `desktop-m4` in the precomputed
startup-cache contract. Focused cache tests and syntax checks pass. A new
signed rootfs was built through all eight phases at
`target/debian-riscv/desktop-m4-cached-20261017-r2/rootfs`; it contains the
static sysusers, ld.so, journal, and fontconfig artifacts.

The GL-QEMU replay with this image still reached only stage1
`handoff-enter action=exec` within the 60-second diagnostic boundary. It
produced no systemd, Xorg, virgl, pixel, or recovery marker. The authoritative
receipt is
`docs/porting/evidence/2026-10-17-qemu-drm-startup-cache-r17.md` and
`/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r17/result.json`.
This closes the rootfs-cache hypothesis as insufficient by itself; the next
QEMU step is a small root-init/systemd exec ABI diagnostic covering console,
proc/sysfs and device handoff. Task 5 remains open and the physical board is
still untouched for display/DRM.

## 2026-10-17 stage1 proc/sysfs fix and runtime boundary (r18–r20)

The stage1 systemd handoff now mounts proc and sysfs before entering the root
filesystem. The native stage1 self-test passed with the expanded ordered
sequence, and r18 reached a real Debian systemd 257 banner with the expected
`riscv64` architecture. This closes the previously missing proc/sysfs handoff
contract, but it does not close the desktop gate: r18 stopped before any
Xorg, DRM topology, virgl pixel, or recovery marker. The diagnostic r19 run
was bounded and stopped before a systemd banner; it is not a runtime verdict.

r20 did not launch QEMU. The host spent more than ten minutes repeatedly
hashing and copying the 1 GiB ext2 run image (about 8.8 GiB read) and was
terminated while still in uninterruptible I/O. This identifies a host-side
preparation cost that must be reduced or bypassed with an already validated
run disk before the next guest replay. The complete evidence, exact input
hashes, and per-run verdicts are in
`docs/porting/evidence/2026-10-17-qemu-drm-procfix-r18-r20.md`.

Historical phase note: Task 5 was temporarily at the guest
systemd-to-Xorg boundary after r20/r21 stopped in slow host root-image
preparation. The r24 replay below supersedes that open runtime boundary; the
host-latency issue remains recorded as a performance concern, not as a failed
DRM/Xorg result. The physical board remains untouched.

## 2026-10-17 QEMU DRM/Xorg VT handoff and recovery (r24)

The r23 runtime reached systemd and the DRM desktop service but Xorg failed at
`VT_ACTIVATE` because the guest has no working virtual-terminal switch. The
M4 session now passes `-novtswitch -keeptty` in both Xorg launch paths. The
source contract and shell syntax checks passed.

The corrected rootfs was replayed once in the GL-capable QEMU container with
networking disabled. The run passed the complete QEMU Task 5 contract:
DRM-only handoff without `/dev/fb0`, `card0`/`renderD128`, libdrm virtio
topology, modesetting `kmsdev=/dev/dri/card0`, Xorg `renderer=virgl`, GBM
white/black pixel probe, the 1280×800 X11 screenshot hash, desktop-ready
marker, and automatic OpenSBI/U-Boot recovery. `result.json` reports
`passed=true`.

The raw serial/result hashes and exact input identities are in
`docs/porting/evidence/2026-10-17-qemu-drm-vtfix-r24.md`; the raw files are
under `/mnt/mydisk/asterinas-gates/qemu-drm-recovery-20261017-r24/`. This
closes Task 5 in QEMU. Task 6 remains open until a real Firefox guest reports
WebRender with a verified virgl renderer, fixed scene pixels, frame evidence,
and restart recovery. The physical board remains untouched.

## 2026-10-17 Firefox builder cross-image preflight

The locally available `codex/asterinas-cross-builder:aa6f7533` image was
checked as a possible Task 6 input. It has a RISC-V GCC and sysroot, but
`rustc --target=riscv64gc-unknown-linux-gnu` fails with `can't find crate for
std`, and no RISC-V GTK development library is present. It is therefore not a
Firefox builder. The exact digest and output are in
`docs/porting/evidence/2026-10-17-firefox-cross-builder-preflight.md`.

Task 6 remains open at the authenticated Firefox build boundary; no Firefox
package or WebRender claim is made. The physical board remains untouched.

The follow-up preflight showed that the host proxy can install the missing Rust
RISC-V target and that Debian Trixie publishes the required RISC-V GTK/DRM/X11
and LLVM packages. Directly mixing those packages into the Ubuntu cross image
does not resolve a coherent libc/sysroot closure, so the next build step is a
separate Debian Trixie cross image rather than a dependency bypass.
The clean Debian image preflight further showed that amd64/riscv64 packages
must come from one pinned snapshot; the live archive currently produces a
`libpcre2`/libc version conflict.
The pinned snapshot is reachable directly from the host but not reliably
through the container proxy; the RISC-V index must be prefetched or served by
a local mirror before constructing the cross sysroot.

The pinned `InRelease`, amd64 index, and riscv64 index are now prefetched and
SHA-256 verified under `/mnt/mydisk/firefox-builder-snapshot-20261017/`.
Matching GTK/DRM/libc package versions are visible for both architectures;
the next step is local-index package resolution and sysroot assembly.
A bounded clean-container resolver downloaded both indexes but remained in APT
package-state processing without a result; it was terminated and no package
installation was attempted.
The complete sysroot closure is extracted and the Firefox ESR `.dsc` plus
Debian tarball are hash-verified; the next gate is binding Build-Depends and
the patched source tree to this cross environment.
The pinned ESR source is now unpacked and both virgl/DRM compatibility patches
apply with zero fuzz; compilation remains the next gate.
The complete closure is now extracted into a 1.5 GiB RISC-V sysroot. A
read-only cross preflight links a GTK/libdrm/X11 RISC-V executable and compiles
a Rust `riscv64gc-unknown-linux-gnu` metadata probe successfully; Firefox
source compilation remains the next gate.
The first offline resolver run stopped on the virtual GTK dependency
`gir1.2-atk-1.0-dev:riscv64`; the resolver now indexes Debian `Provides`
aliases and produces a verified 391-package riscv64 closure without a
substitution. Its hash and path are recorded in the Firefox preflight receipt.
The bounded downloader verified 124 packages (44 MiB) before snapshot HTTP
502 responses forced a fail-closed stop; partial files were removed.
The Debian mirror-pool retry then verified 369 of 391 packages (244 MiB); two
large LLVM/Z3 files stalled and were stopped with no partial files retained.
The final Z3 package was fetched from a secondary mirror; the reuse-aware
downloader now reports `SNAPSHOT_DOWNLOAD PASS packages=391`. The complete
verified package closure is retained on `/mnt/mydisk` and is ready for sysroot
extraction.
The cross image still lacks host tools such as debhelper, lld, cbindgen,
nodejs, nasm, xvfb, and xauth; no mutable image was created because the live
Ubuntu archive was unavailable during the bounded simulation.
Those host tools and a saved Rust RISC-V standard-library component now form a
disposable cross toolchain; the combined Rust/GTK/libdrm/X11 preflight passes.
An isolated amd64 host-tool closure is now extracted on `/mnt/mydisk`; the
remaining preflight failure is only the flaky Rust standard-library mirror
download, before any Firefox compilation.

The next bounded Task 6 step closed the pinned package-identity gate. The
complete ESR Build-Depends resolver produced 405 RISC-V target rows and 553
amd64 host rows; all target archives and all host archives were downloaded and
size-checked, and `dpkg-checkbuilddeps` passed against a temporary metadata
database without `-d`. The focused render-node, libdrm-topology, and
modesetting host regression suite passed 22 tests. This is still a build
preflight, not a Firefox/WebRender runtime receipt: Firefox compilation and
the strict `renderer=virgl` + WebRender gate are the next acceptance boundary.
No physical-board operation was performed.

The Task 6 build boundary then moved from preflight into a real RISC-V
cross-build. The initially unpacked source was rejected after finding 51,550
zero-byte files; a fresh source tree from the verified orig/debian tarballs
was created and patched. Configure now completes for the RISC-V target,
including GTK/DRM/X11, Rust, LLVM/bindgen, Node, and WASI. The build has
compiled substantial Rust and C++ portions with no compiler error and leaves
an 11.2 GiB incremental `build-browser` tree. The bounded build was stopped
twice and is resumable with `debian/rules build-arch`; no Firefox binary,
WebRender receipt, or physical-board result exists yet. The next gate is to
finish the incremental build and package the resulting RISC-V Firefox before
running the strict QEMU WebRender/virgl runtime gate.

## 2026-10-17 Firefox package admitted; DRM desktop compatibility rerun (r14)

The incremental cross-build produced a RISC-V Firefox ESR package. The
unstripped `libxul.so` carried debug information and could not fit the fixed
M4 image, so the package was rebuilt after a deterministic `--strip-debug`
step. The package provenance gate, runtime provenance lock, and a new
2-GiB `desktop-m4-firefox` frozen profile all pass. The stable stage1 label is
retained so the existing boot artifact continues to recognize the root; the
profile remains distinct by UUID, size, and manifest identity.

The GL-capable QEMU replay with this rootfs passed the entire DRM desktop
contract and recovery. It recorded DRM-only handoff, `card0`/`renderD128`,
`renderer=virgl`, the fixed GBM white/black probe, the 1280×800 X11 pixel hash,
the desktop-ready marker, and a fresh OpenSBI/U-Boot epoch. The authoritative
receipt is `docs/porting/evidence/2026-10-17-qemu-firefox-rootfs-drm-r14.md`;
raw files are under
`/mnt/mydisk/firefox-builder-snapshot-20261017/qemu-rootfs-firefox/gate-firefox-drm-r14/`.

This closes the Firefox-rootfs compatibility prerequisite while leaving Task 6
open: the next gate must launch the actual Firefox binary in the guest and
prove WebRender's renderer is virgl (never llvmpipe), capture a fixed browser
scene/frame hash, and verify bounded recovery. The physical board remains
untouched.

## 2026-10-21 update: RockOS vendor reference stack

A one-shot U-Boot boot of the packaged
`/boot/vmlinuz-6.6.87-win2030-origin` kernel (the image whose vermagic
matches the vendor `pvrsrvkm.ko`) brought up the PowerVR AXM-8-256 on the
physical board: `card1`/`renderD128`, `rgx.fw/sh.30.3.408.101` loaded, and
Xorg glamor enabled on `PowerVR A-Series AXM-8-256`. The default extlinux
entry was not changed. This is a RockOS reference receipt, not an Asterinas
result; the native port decomposition is in
[2026-10-21-megrez-powervr-native-acceleration.md](2026-10-21-megrez-powervr-native-acceleration.md)
and the raw evidence in
[2026-10-21-rockos-powervr-reference.md](../../porting/evidence/2026-10-21-rockos-powervr-reference.md).

The board-hosted QEMU/virgl route remains no-go for a different reason than
the S13 run: a render node now exists, but QEMU `egl-headless` still fails
`eglChooseConfig (core mode)` against the vendor GLES-only Mesa.
