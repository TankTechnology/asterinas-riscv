# E3 NewSession history audit r109

## Scope

This is a read-only audit of retained records. It does not start QEMU,
Firefox, a guest, or hardware. It resolves the apparent contradiction that a
three-field capability was said to have succeeded in r34.

## Finding

The retained r34 record is **not** a successful `WebDriver:NewSession`
receipt. `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r34.md`
states that the scene produced 32 DOM timer samples but reported
`webgl_context=0`, `webgl_debug=0`, and
`FIREFOX_DRM_FAIL_REASON`; it contains no `new_session_done` marker or
response-header evidence. Therefore it cannot be used as a successful
three-field control.

The actual two-arm runtime comparison is r101. Its A (minimal) and B
(legacy/three-field) arms used the same kernel, DTB, rootfs and host gate.
Both reached GLX `virgl`, Marionette greeting, and completed the request send;
both then received no response header by 360 seconds. The envelope arm r107
reproduced the same boundary at 240 seconds. This closes the capability-shape
hypothesis without another runtime attempt.

The retained same-host Linux control is not a successful counterexample
either. It used a Linux guest with a devtmpfs shim and headless Firefox; its
later greeting succeeded, but the same three-field request received no
response header by 40 seconds. It differs in kernel, boot path, display and
budget, so it is not an Asterinas equivalence test, but it does not support
the claim that the three fields alone guarantee success.

## Comparison table

| Record | Guest/input | Capability | Greeting | NewSession response | Interpretation |
|---|---|---|---|---|---|
| r34 retained scene | Asterinas, scene gate | not recorded as a NewSession control | not a receipt | no marker; scene failed WebGL boundary | not evidence of success |
| r101-A | Asterinas, same inputs as B | minimal `strictFileInteractability` | pass | absent at 360 s | capability arm A fails |
| r101-B | Asterinas, same inputs as A | three legacy fields | pass | absent at 360 s | capability arm B fails |
| r107 | Asterinas, W3C envelope | three fields in `alwaysMatch` | pass | absent at 240 s | envelope does not change boundary |
| Linux retained control | Linux guest, headless/devtmpfs | three legacy fields | pass on later attempt | absent at 40 s | non-equivalent control also stalls |

## Decision

Do not revert the scene gate to the three-field shape on the basis of r34.
The next diagnostic boundary remains Firefox-specific Content/Marionette IPC
after `send_complete`; generic futex, ppoll, epoll, socketpair and SCM_RIGHTS
probes already pass in r105/r108. No full scene or hardware run is justified
by this audit.

## Sources and hashes

- `docs/porting/evidence/2026-10-16-qemu-firefox-drm-scene-r34.md`
  SHA-256 `a7d1dda0f5f588092bddb246aef00849ae300c8ec68d22453557753d7ff16b1d`
- `docs/porting/evidence/2026-10-20-e3-firefox-newsession-capability-ab-r101.md`
  SHA-256 `66713c86e6d122746ee76fcc11e2b0c2d8a1d269bd34a8282708cbfb678d6e3c`
- `docs/porting/evidence/2026-10-20-e3-firefox-newsession-envelope-r107.md`
  SHA-256 `370627cfb5b9fe34641be2ba512884596fa136f0bd47d64419c611960b713ca2`
- `docs/porting/evidence/2026-09-16-browser-interaction-performance-baseline.md`
  SHA-256 `0d5e26fc5a504092b1f682b15b44530f991081365d79c1ed8efc3816ce26c4ed`
- `docs/porting/evidence/2026-09-16-board-rockos-linux-firefox-devtmpfs.log`
  SHA-256 `58a3032e18b1dcf4c9f2e171161da83c388f17ed7263bdc546a9dabe5e3372fb`

