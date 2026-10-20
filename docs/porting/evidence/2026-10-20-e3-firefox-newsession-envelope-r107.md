# Firefox NewSession W3C envelope probe r107

- gate: `e3-firefox-newsession-envelope`
- claim: E3 diagnostic only; this is not a full WebRender scene result
- status: `fail` at the NewSession response boundary

## Scope

One bounded QEMU probe used the same official Debian riscv64 Firefox rootfs,
kernel, U-Boot, DTB, Stage1 initramfs, package lock and package checksums as
the prior capability runs. The only protocol variable was the W3C capability
envelope:

```json
{"capabilities":{"alwaysMatch":{"acceptInsecureCerts":false,"pageLoadStrategy":"none","strictFileInteractability":true},"firstMatch":[]}}
```

The probe did not start the Firefox scene, did not modify the Firefox binary,
and did not access real hardware.

## Result

| boundary | result |
| --- | --- |
| GLX renderer / render node | pass: `virgl`, `/dev/dri/renderD128` |
| Marionette greeting | pass |
| `WebDriver:NewSession` send | pass: `send_complete` |
| `WebDriver:NewSession` response header | **absent before 240 s** |
| terminal probe marker | absent; QEMU recovered by the bounded host cleanup |

The terminal process samples again show Firefox alive while the request is
outstanding; the response absence is therefore not a TCP-connect failure. The
W3C envelope does not unlock E3. Together with r101's minimal/legacy A/B, this
rules out the capability shape tested so far as the primary variable.

## Inputs and artifacts

| artifact | path | SHA-256 |
| --- | --- | --- |
| root image | `/mnt/mydisk/asterinas-firefox-rootfs-20261020-envelope-r107/debian-root.ext2` | `e1feafbced3fe603ff72e17c9ed3e40ac396d1979a6b17427b92acf903c71611` |
| root manifest | `/mnt/mydisk/asterinas-firefox-rootfs-20261020-envelope-r107/rootfs-manifest.json` | `6075bf5f8bd64e095570357dae76fabefa81ad9cfc797e8cb6f26d3b0dc70d64` |
| overlay manifest | `/mnt/mydisk/asterinas-firefox-rootfs-20261020-envelope-r107/dev-overlay-manifest.json` | `a06d80579f47edb3964de6dad974dcbf24d314bc2ef2af921c5827eec5934459` |
| lifecycle | `/mnt/mydisk/qemu-firefox-probe-envelope-r107/lifecycle.json` | `8ac46df86b1bb7d40981e270e8c47e2b90bc488a547568a9e34753c5f679bb82` |
| result | `/mnt/mydisk/qemu-firefox-probe-envelope-r107/result.json` | `62c6d859e8907a29dc025c57de853d633ee3b5e376da4444fc2569d3864917d8` |
| serial log | `/mnt/mydisk/qemu-firefox-probe-envelope-r107/desktop-m4-drm.serial.log` | `4c108e7a288a1e02f910afc81fadab4fb896e4efc19048c204a1676132078955` |

## Verification

- Focused Firefox gate tests: 35 passed.
- Persistent-container `firefox_fast_check.sh`: 337 tests passed and emitted
  `FIREFOX_FAST_CHECK_PASS`.
- Python compilation and `git diff --check`: passed.
- No full 900-second scene was run.

Next investigation remains the Firefox-specific parent/worker IPC wait, not
another capability encoding or a full scene retry.
