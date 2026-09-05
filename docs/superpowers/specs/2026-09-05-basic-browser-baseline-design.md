# Basic Firefox Browser Baseline

## Goal

Establish a deterministic, repository-controlled acceptance target for basic
Firefox desktop use on RISC-V.  The target is intentionally independent of
third-party anti-bot pages such as Baidu or Bilibili.

## Scope

The controlled fixture must demonstrate HTTPS navigation, Latin/CJK text,
PNG image rendering, HTML form submission, same-origin page navigation,
basic JavaScript execution, cookie/localStorage access, Fetch, and a bounded
file download.  Complex JavaScript frameworks, WebAssembly, video playback,
and third-party web-site compatibility are out of scope for this milestone.

## Test layers

1. Host-side unit tests validate the fixture contract, evidence schema, and
   failure classification.
2. QEMU SMP=4 runs the fixture through the proxy network path and then the
   direct network path.  Each run records bounded serial phases, screenshots,
   process-security evidence, and an immutable input manifest.
3. After both QEMU paths pass, one controlled Megrez run starts the same
   Firefox/Xorg profile.  The run has a bounded timeout and collects serial,
   desktop screenshot, and process state; it does not trigger repeated resets.

## Acceptance

The QEMU and physical runs pass only when all in-scope fixture operations
complete with the expected URL, title, DOM fields, JavaScript result, image
dimensions, storage value, and downloaded-file checksum.  Any failure is
classified as network, TLS, Firefox/Marionette, Xorg, or kernel/boot before a
physical retry is considered.

## Non-goals

No acceptance claim is made for CAPTCHA handling, modern third-party sites,
video, WebAssembly, or full browser feature parity.  Those capabilities can
be added as separate milestones after the basic desktop/network path is
stable.
