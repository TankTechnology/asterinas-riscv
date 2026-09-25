# Megrez fixed-mode native scanout: first selected-board gate

This is an opt-in display-controller experiment, not full KMS, GPU rendering,
or a demonstrated Firefox speedup. Commit `ef51923c3` adds
`asterinas.dc_native_scanout=1`. It keeps the firmware-programmed 1920 × 1080
HDMI mode and writes only the EIC7700 primary-plane address after cleaning the
non-coherent GEM buffer. The default remains the tested firmware-copy backend.

The candidate accepts only the observed linear controller configuration
`0x18004011`, a matching BGRX8888 firmware framebuffer, a 7680-byte pitch,
and a 64-byte-aligned DMA address whose entire frame fits a 32-bit plane
address. Constructor failure logs a reason and uses firmware copy. Full
presentation cleans the entire frame; `DIRTYFB` on the already displayed
front buffer cleans the submitted damage rows, and an empty damage list means
the full frame. A new buffer is always cleaned in full. The backend retains
the displayed VMO. Unsupported `PAGE_FLIP` flags now return `EOPNOTSUPP`
because the driver does not deliver flip-complete events.

This path has no vblank wait or verified shadow-register commit. Address
readback proves only that the MMIO register accepted the write. It does not
prove that the panel displayed the frame, that writes were coherent on all
harts, or that the frame was tear-free. The GEM mmap remains write-back and
Xorg repaints a mapped front buffer, so tearing is possible even when cache
cleaning is correct.

## Preflight and selected boot

The RISC-V/Sv39/SMP4 release build passed. Two host-executed tests of the
shared DMA address/geometry contract passed. The 1920 × 1080 QEMU firmware
display gate passed all six stages with this Image and the flag absent; QEMU
does not model the EIC7700 controller or its DMA. The Image SHA-256 was
`7e78cadb6204a7804763726ad1784087eb44b6b6ca5cf1b507ad799e98aa7628`.

The selected boot used the unchanged prepared DTB
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and Stage1
`ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
Each artifact's local and remote size and SHA-256 matched in
`/home/debian/asterinas/native-scanout-ef51923c3/`. U-Boot checked loaded
length and CRC32 before the temporary `booti`; the persistent boot entry
remains RockOS. The stable serial device was
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.

The previous root console confirmed boot ID
`7e02d435-fc02-4a1e-99fe-f460460c1eb3` before software reboot. The
candidate boot ID was `abd85de3-dec1-4bef-8b10-545f36917eb5`; nonce-framed
commands confirmed UID 0 and the same ID after the host closed and reopened
the serial connection. Xorg reached 1920 × 1080, Firefox's service reported
`MainPID=126`, `NRestarts=0`, and `SubState=running`, and the recovery
watchdog was disarmed only after those checks. The root console later
remained responsive after an overlong batched X11 probe was interrupted.

The kernel's native counters showed the GEM physical address
`0xf8001000`, not the firmware framebuffer at `0xfd800000`. At 31 dirty
notifications, cumulative cache-clean time was 80.480 ms; at 127 it was
297.532 ms. The sampled underflow status remained false. These milestones
span idle and interactive work, so their difference is not a clean latency
comparison. One later set of five `PageDown` inputs completed in a bounded
command; Xorg process `schedstat` run time rose by 382.830 ms and Firefox's
by 141.612 ms. There was no new native counter milestone during that short
window. The page URL, HDMI pixels, and input-to-visible-frame latency were
not independently captured; no user-visible speedup is claimed.

Raw evidence is under
`/home/ubuntu/.codex/asterinas-native-scanout-20260926/`: the board manifest,
candidate serial/result files, root-console sample, QEMU gate result, and
RockOS GPU inventory. The candidate remains reachable through its root serial
console; an ordinary reboot returns to RockOS.

## Next gate

Verify physical pixels on HDMI against a controlled checkerboard or direct
observation, then exercise a short dirty rectangle and a full-frame update.
Only after this check should the native path become a default. For tear-free
presentation, add a second pinned buffer, a real vblank/flip-completion path,
and truthful DRM events and buffer lifetime. If cache cleaning still dominates
small updates, reduce synchronization work using measured damage patterns
without weakening coherency.
