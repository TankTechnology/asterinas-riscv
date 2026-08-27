# Debian M6 Browser Evidence Design

## Goal

Extend the existing Asterinas Debian M5 QEMU gate with honest browser-level
evidence: NetSurf must foreground a Baidu document before the first screenshot,
and a local page must classify the packaged JavaScript engine separately before
the second screenshot. Network success remains independent from JavaScript.

## Existing boundary

M5 already proves, through Asterinas rather than a Linux guest kernel, that QEMU
slirp DNS resolves `www.baidu.com`, HTTPS returns status 200 from guest address
`10.0.2.15`, and the M4 application desktop reaches its ready marker. The M5
framebuffer capture is non-blank but currently shows PCManFM. It therefore does
not prove that NetSurf foregrounded or rendered the remote document.

The Debian RISC-V `netsurf-gtk 3.11-2` binary contains the Duktape bindings and
the `enable_javascript` option. It also links the expected PNG, JPEG, WebP, SVG,
TLS, and libcurl libraries. Its JavaScript surface is not equivalent to a modern
Chromium engine and must not be presented as such.

## Considered approaches

1. **Guest window control plus two host screenshots (selected).** Add `xdotool`
   to the frozen M5 identity. A bounded guest evidence service activates the
   NetSurf window, verifies a Baidu-derived window title, emits a remote marker,
   pauses for the host screenshot, navigates to a local JavaScript fixture, and
   emits one classified status. This is deterministic and testable without
   assuming a Matchbox stacking order.
2. **QEMU HMP keyboard input only.** Repeated Alt-Tab and typed URLs avoid one
   package but depend on focus, stacking order, keyboard layout, and timing. A
   screenshot could select the wrong window without explaining why.
3. **A browser remote-debugging protocol.** This would offer strong DOM evidence
   but NetSurf has no Chromium-style protocol, and adding a modern browser is a
   separate large distribution milestone.

## Guest components

The M5 profile adds `xdotool` to both requested and identity packages. The M4
session keeps its local welcome fallback, but when the trusted M5 URL file is
present it launches NetSurf with `--enable_javascript=1` and the URL as one
quoted argument.

The root image contains a fixed local HTML fixture. Its static title is
`ASTERINAS_JS_PENDING`; a small inline script changes both the title and a
visible DOM token to `ASTERINAS_JS_PASS`. No external resource participates in
this smoke test.

A new bounded M6 evidence script runs only after the M5 network service and M4
desktop evidence have completed. It:

1. locates one visible NetSurf window using `xdotool`;
2. waits for a window title containing either `baidu` or `百度`;
3. activates that exact window and emits a stable remote-render marker;
4. waits five seconds so the host can capture the foreground remote page;
5. uses `xdotool` to navigate that same window to the local fixture;
6. reports `limited-pass` when the title becomes `ASTERINAS_JS_PASS`, `failed`
   when the static page loads without the title transition, or `disabled` when
   the session explicitly did not request JavaScript;
7. emits one final marker carrying the same status.

Any remote-title or window-control failure fails the M6 browser gate. A
JavaScript status never fails the already-proven network/browser gate.

## Host gate and artifacts

The M6 adapter subclasses the M5 QEMU adapter and reuses the same Asterinas
kernel, generic-Sv39 SMP=4 CPU contract, VirtIO network/input/block devices,
Bochs display, descriptor-pinned artifacts, deadlines, and process-group
cleanup.

The inherited protocol stops at the remote-render marker and captures
`desktop-m6-browser.ppm`. The M6 extension then waits for the JavaScript status
and final marker and captures `desktop-m6-javascript.ppm`. Both images use the
existing bounded PPM validator. `result.json` records the JavaScript status and
metadata for both screenshots. The complete drained serial transcript must
contain the M5 network markers, M4 desktop markers, remote-render marker,
exactly one allowed JavaScript status, and matching final marker in order.

## Testing and evidence

Host tests freeze package identity, local fixture contents, command quoting,
window/title handling, all three JavaScript classifications, ordered transcript
classification, two-screenshot lifecycle, Make target, and failure markers.
Every production behavior is introduced through a failing test first.

Runtime validation rebuilds only the signed M5 rootfs, verifies the frozen
manifest and package lock, then runs one Asterinas QEMU M6 gate with a 300-second
cold-boot budget. Completion requires `passed:true`, two bounded non-blank
screenshots, a remote Baidu title marker, and an explicit JavaScript status.

## Non-goals

- Claiming Chromium compatibility or general modern-web compatibility.
- Making a changing Baidu pixel hash part of the contract.
- Replacing the physical RJ45/GMAC evidence path.
- Treating ICMP or rtnetlink compatibility as browser-network prerequisites.
- Adding Chromium, Firefox, DRM acceleration, audio, or video playback.
