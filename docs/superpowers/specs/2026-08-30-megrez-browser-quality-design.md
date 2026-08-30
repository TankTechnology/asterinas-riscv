# Megrez Lightweight Browser Quality Design

## Goal

Make the existing Debian/NetSurf desktop useful for ordinary lightweight Web
browsing on Asterinas.
Use QEMU for broad, repeatable coverage and reserve Megrez for one bounded,
high-information acceptance boot per milestone.

The milestone covers HTTPS pages, Chinese text, raster images, address-bar and
form input, scrolling, navigation history, a small download, and sustained
browsing.
It does not claim Firefox or Chromium compatibility, modern JavaScript parity,
video playback, login workflows, or Web applications.

## Current baseline

The current signed RISC-V root image contains NetSurf 3.11 and WenQuanYi
MicroHei.
An existing QEMU screenshot renders the Baidu mobile page and Chinese text.
A physical Megrez run has already proved the following sequence under
Asterinas:

- Debian root handoff and Xorg at 1920x1080;
- static-RJ45 HTTPS access to `www.baidu.com` with status 200;
- a Baidu-hosted PNG rendered in a foreground NetSurf window;
- the Baidu mobile homepage identified by its X11 title;
- an `asterinas` search whose result page was identified by its X11 title;
- automatic return to U-Boot after the bounded guest recovery timer.

The same physical run did not enumerate a pointer device.
The browser evidence was therefore valid, while the full interactive desktop
result remained deliberately degraded.

## Alternatives

### Selected: exhaustive QEMU gate and one physical acceptance boot

Run deterministic browser mechanics and live-page compatibility in QEMU.
Only a frozen QEMU-passing artifact set may reach Megrez.
The physical run repeats a compact subset that detects hardware-specific
network, input, framebuffer, timing, and recovery differences.

This gives fast failures, reproducible screenshots, and few board resets.

### Rejected: mirror every QEMU case on Megrez

This provides redundant coverage but turns every browser issue into a slow
serial experiment.
It also increases the probability of a board hang without improving failure
localization.

### Rejected: manual exploratory browsing as the primary gate

Manual use remains useful after an automated pass, but it has no stable
deadline, transcript, or reproducible success contract.
It must not replace the automated gate.

## QEMU quality gate

The gate boots the same Asterinas kernel, signed Debian root image, desktop
session, and NetSurf package intended for Megrez.
One deterministic host fixture supplies fixed pages and downloads; the live
Baidu mobile page remains a separate external-network check.

The deterministic fixture contains:

- a Chinese and Latin text page using common font weights and sizes;
- a CSS layout with nested blocks, a long scroll region, and raster images;
- a GET form whose result title contains the submitted query;
- two linked pages for back and forward navigation;
- a bounded downloadable file with an exact size and SHA-256 identity.

The guest drives NetSurf through X11-visible input rather than internal test
hooks.
It records process identity, one unambiguous window, window titles, download
identity, and ordered milestone markers.
QEMU HMP captures a screenshot after the fixture page, Baidu homepage, Baidu
search result, and final navigation state.

The gate fails on browser exit, ambiguous windows, title timeouts, failed
download identity, missing screenshots, guest panic, or any reordered marker.
Remote-site challenge pages are classified separately from Asterinas or
NetSurf failures.
The full QEMU run should remain below eight minutes.

## Physical acceptance boot

The physical run starts only when unit tests and the QEMU gate pass against
the exact artifact hashes in the immutable Megrez plan.
It performs one read-only preflight before `booti`:

- acquire the serial device exclusively and stop at a fresh U-Boot prompt;
- verify planned kernel, initramfs, DTB, and root-image identities;
- verify the host fixture, RJ45 proxy, and evidence receiver are reachable;
- record U-Boot USB inventory without treating it as proof of Linux HID
  enumeration.

One Asterinas boot then performs this bounded sequence:

1. bring up Debian, Xorg, the window manager, and NetSurf;
2. require the static-RJ45 stress, HTTPS, and image milestones;
3. classify keyboard and pointer devices independently;
4. load the Baidu mobile homepage and submit the `asterinas` search;
5. load the deterministic text/layout page, scroll it, and follow one link;
6. exercise back and forward navigation;
7. download the fixed small file and verify its SHA-256 inside the guest;
8. keep the browser and network active for a bounded soak interval;
9. upload a compressed root-window capture and its SHA-256 to the restricted
   host evidence receiver;
10. wait for `asterinas.reboot_after=600` to return to U-Boot.

The host uses `--timeout 900` because the Megrez guest clock can advance more
slowly than host monotonic time.
The short DesignWare hardware watchdog is not used for desktop acceptance.
No successful or failed run requires a normal manual reset.

The browser-content result and the interactive-input result are separate.
Missing pointer hardware may publish a content pass with an input-degraded
classification, but a full interactive pass requires both keyboard and
pointer evidence.

## Evidence and failure localization

Every run binds evidence to the immutable plan hash and publishes the result
last.
The evidence set contains serial output, ordered milestones, process and
window identities, network and download hashes, screenshots, the physical
capture, and fresh U-Boot recovery.

Failures map to one boundary:

- a QEMU failure blocks physical execution;
- a physical failure before Xorg indicates an Asterinas or hardware boundary;
- HTTPS or download failures indicate GMAC, TCP, clock, proxy, or fixture
  boundaries;
- correct titles with an incorrect screenshot indicate NetSurf layout, CSS,
  or font behavior;
- missing input evidence indicates USB HID, evdev, or Xorg input behavior;
- missing U-Boot recovery indicates the recovery path, not browser quality.

One physical retry is allowed only when evidence proves a transient transport
or fixture failure.
Otherwise the issue returns to a deterministic host or QEMU reproducer before
another board boot.

## Implementation boundaries

Reuse the existing M5 network, M6 browser, M7 Baidu, Megrez debug, and atomic
evidence-publication components.
Add one browser-quality fixture, one guest evidence driver, and one thin host
gate/classifier.
Extend the existing restricted host fixture for a size-bounded screenshot
upload instead of creating a general file server.

Root-image package changes are limited to a standard X11 capture utility if
the current image cannot capture the root window.
No kernel change is accepted unless the QEMU or physical evidence isolates a
Linux-compatible contract missing from Asterinas.

## Verification

Development follows RED-GREEN-REFACTOR tests for the fixture, guest marker
contract, download identity, upload bounds, classifier, and failure cleanup.
The final verification order is:

1. focused host unit tests and static checks;
2. one full QEMU browser-quality gate with all screenshots;
3. artifact identity freeze and read-only physical preflight;
4. one physical Megrez acceptance boot;
5. evidence audit before any usability claim.
