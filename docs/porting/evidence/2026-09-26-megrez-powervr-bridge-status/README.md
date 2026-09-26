# Megrez PowerVR reference bridge status

This is a **RockOS reference result**, not Asterinas GPU rendering. The selected
RockOS `6.6.87-win2030` kernel loaded the matching PowerVR firmware and exposed
`/dev/dri/renderD128`. The unchanged `tools/riscv/drm/gles-pixel-probe.py`
returned `PowerVR A-Series AXM-8-256`, with white left pixels and black right
pixels. It exited successfully under a 30-second bound.

The opt-in `ioctltrace` build from source SHA-256
`3cc1c0a3c695b00c02df4820f338d835702d72e5bf001c29eabddaa18621c898`
recorded the outer `PVR_SRVKM_CMD` return and the vendor bridge's inner
`eError` output. All 188 calls across 26 distinct functions had outer return
zero **and** inner status zero. The full bounded [trace](bridge-status.log),
[summary](summary.json), and [probe result](result.json) are included. The trace
SHA-256 is `a06eed0aa1a50c413b25781f4e375d99ac0daed61b85144b7739bf6ed62a12c5`;
the complete on-board trace (including non-bridge lines) had SHA-256
`9282049aa984fd476b06e1415fde6e7371485a0243dc1744e81e68135a9c9360`.

The reference software is RockOS `eswin-eic7x-gpu 24.2+0rockos3` with Mesa
packages `1:22.3.5+1rockos1+0pvr2`. The pinned files on that installation are:

| File | SHA-256 |
| --- | --- |
| `/lib/firmware/rgx.fw.30.3.408.101` | `25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b` |
| `/lib/firmware/rgx.sh.30.3.408.101` | `9b7a9779c982ef3c29be2f77378c6cee553bba67b` |
| `/usr/lib/libVK_IMG.so` | `a8ff3f0cc60696de1594ca11accd2947078945c422e39ee917486a3c5d5d55a8` |
| `/usr/lib/libGLESv2_PVR_MESA.so` | `15aed7512e02399a6836968b5d9d6b37a69f1cc355135c212e7799b2e2e26f2f` |
| `/usr/lib/riscv64-linux-gnu/libEGL.so.1.1.0` | `8f3e33f71e968c5d1d3a1dcb23a6a80848919224b74aa6be69004e07eb1b2e9f` |
| `/usr/lib/riscv64-linux-gnu/libGLESv2.so.2.1.0` | `5a6436745b67a259b937a72b3b6675cb867a51ff0df57a7dd5d523c0e26272fe` |

Licensed firmware and userspace libraries stay on RockOS; they are not copied
into Git. The Asterinas PowerVR powered-ID probe only proves BVNC
`30.3.408.101` and reversible clock/reset control. Asterinas has not yet
started firmware, submitted a render job, or accelerated Firefox. The required
owner, DMA, bridge, and display-sharing work remains in the
[implementation plan](../../../superpowers/plans/2026-09-26-megrez-powervr-minimal-render.md).
