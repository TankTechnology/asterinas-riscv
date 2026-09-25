# EIC7700 display handoff: selected physical diagnostic

The `asterinas.dc_probe=1` diagnostic in
`kernel/src/device/dri/eic7700.rs` reads the enabled `eswin,dc` node's
third register aperture only after validating its address and size. It reads
five controller registers when Xorg first allocates the DRM dumb-buffer pool;
it does not write registers or change the firmware scanout backend.

The release RISC-V/Sv39/SMP4 Image SHA-256 was
`c9b3ab900e4be90949d0c704a0e1ad444ed19386eeab9b7c73320f73d947b713`.
The selected boot used the unchanged prepared Megrez DTB
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and Stage1 `ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
It was loaded from new RockOS partition-3 files under
`/home/debian/asterinas/display-probe-47f9187e0/`; each file's size and
SHA-256 were verified before and after the directory's atomic publication,
and U-Boot checked byte count and CRC32. The default boot entry and prior
candidate were untouched; an ordinary reboot still selects RockOS.

The new Asterinas boot ID was `fbbfc880-807c-4983-a95a-75a4ab1c46bf`.
The root debug console returned UID 0 and this boot ID, and the host closed
and reopened the stable `/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`
port before verifying the same boot again. Xorg reported 1920 × 1080,
Firefox's service had `MainPID=131`, `NRestarts=0`, and `SubState=running`,
and the 420-second software recovery timer was disarmed after admission.

The kernel logged:

```text
ASTERINAS_DC_PROBE primary_addr=0xfd800000 stride=7680 config=0x18004011 display_h=0x8980780 display_v=0x4650438 pool_addr=0xf8000000 pool_size=67108864
```

The controller's primary address matches the DTB's firmware framebuffer.
The GEM pool spans `[0xf8000000, 0xfc000000)` and does not overlap the
firmware framebuffer at `0xfd800000`. The low fields of `display_h` and
`display_v` are 1920 and 1080; their high fields are 2200 and 1125. The
primary plane is enabled; the sampled flip-in-progress and underflow bits
are clear. The sampled configuration has shadow-register bit 3 clear, so the
RockOS driver's shadow/commit sequence cannot be copied blindly into this
firmware-owned state.

This inventory rules out an address overlap and shows a plausible 32-bit
physical base, but it is **not** a DMA reachability or cache-coherency test.
The RockOS `es_gem.c` allocator requests `DMA_ATTR_WRITE_COMBINE` and forced
contiguity when the display MMU is not in use. Current Asterinas GEM mmap
returns `Mappable::Vmo`, whose ordinary fault path installs write-back user
pages. This board's DTB marks `eswin,dc` `dma-noncoherent`. Redirecting the
controller to that VMO without a valid CPU-to-device synchronization contract
could show stale pixels even if the address is reachable. The existing OSTD
EIC7700 cache service can flush a checked physical range, but a per-cache-line
flush for every changed frame is not yet measured against the present copy
cost. A direct-scanout candidate must either expose an uncached/write-combine
DMA pool through a lifetime-safe mmap path or demonstrate a correct and faster
range sync. It must also keep the displayed backing alive through vblank.

The matching QEMU firmware-display gate, using the same Image and the
`megrez-sv48-svade-drm-firmware` contract approximation at 1920 × 1080,
passed all six stages: driver, mode, set CRTC, page flip, dirty framebuffer,
and ready marker. QEMU does not model the EIC7700 display registers or DMA
coherency. The local evidence directory is
`/home/ubuntu/.codex/asterinas-display-probe-20260925/`; the QEMU evidence is
under `target/qemu-uboot/display-probe/megrez-board-geometry/evidence/`.
