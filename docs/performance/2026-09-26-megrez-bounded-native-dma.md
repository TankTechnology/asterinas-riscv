# Megrez native scanout: bound the GEM pool to the DC DMA window

The opt-in EIC7700 primary-plane backend accepts only a 32-bit scanout
address. Its original 64 MiB GEM pool was allocated from unrestricted physical
RAM, then each presented framebuffer was checked for a 32-bit span. This left
an avoidable first-mode-set failure if the pool landed above 4 GiB: the
backend had already been selected, so Xorg would receive `EINVAL` instead of
using the firmware presenter. This is a proven allocation-contract gap, not a
proven explanation for the one observed intermittent Xorg failure.

The selected native backend now reserves the entire contiguous pool inside
`[0, 2^32)` before registering the DRM nodes. `VmoOptions::alloc_contiguous_in`
uses OSTD's bounded frame allocation, which checks the whole extent. If this
allocation fails, display selection logs the error and retains the firmware
copy backend. Pool zeroing occurs outside the GEM spin lock. An invalid native
buffer now logs its address, extent, geometry, and pitch at the scanout
validation boundary, so a later `EINVAL` has a concrete cause.

One earlier selected boot of Image
`52b108cff8a1a87cfb3a6eeaa2fce5e57c0c75c88d120656eb05d992311e5f6c`
reported `failed to set mode: Invalid argument` in Xorg, while two subsequent
boots of that Image succeeded. The failed boot did not record its GEM physical
address. Two diagnostic boots with pool-address logging succeeded, both using
`0xf8000000`. Thus the intermittent failure remains open; no root cause or
resolution is claimed for that specific event.

The bounded candidate Image SHA-256 is
`3a3cf4edb05b984be68f6af40603ec2a5a7fdc209cffcff4a79b15588e3a91f4`.
Four focused RISC-V kernel tests passed in QEMU: a multi-page contiguous VMO
stayed inside its requested physical window; empty or too-small windows and a
resizable source were rejected. The resizable-source test failed on the old
implementation before the rejection was added. The
1920 × 1080 firmware display QEMU gate passed all six stages with this Image
and the native flag absent. The physical candidate used the unchanged DTB
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and Stage1
`ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
U-Boot verified sizes and CRC32 values before the temporary `booti`; the
persistent boot entry remains RockOS.

On the selected physical boot, the root debug console proved UID 0 and fresh
boot ID `516751d7-4719-449f-8f6d-59e275602e65`, then proved the same ID after
closing and reopening the host serial connection. The native pool was
`0xf8000000..0xfc000000`; the displayed object began at `0xf8001000`.
Xorg selected 1920 × 1080 without a mode-setting error; Firefox's service
reported PID 132, zero restarts, and `running`. Sampled DC underflow was
false. The Asterinas software recovery watchdog was disarmed only after the
control and desktop checks. A normal software reboot still selects RockOS.

Evidence is at `target/gpu-drm-20260926/board-postreview/` and
`target/gpu-drm-20260926/qemu/megrez-board-geometry/postreview/evidence/`.
These local artifacts contain the board manifest, boot transcript, result,
root-console observation, and QEMU gate result. The monitor was unavailable,
so this does not verify HDMI pixels, tearing, or input-to-visible-frame
latency. Native scanout remains opt-in. PowerVR hardware rendering also
remains unverified: RockOS currently boots a kernel that does not match its
installed `pvrsrvkm` module, and an early-hang recovery path for the matching
package-kernel boot has not been established.
