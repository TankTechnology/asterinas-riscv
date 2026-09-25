# Megrez GEM cache-clean probe before native scanout

## Purpose and implementation

The firmware display backend copies each update from the DRM dumb-buffer VMO
to the bootloader's framebuffer. Before programming the EIC7700 display
controller to scan the VMO directly, a selected boot measured the cost of
making a full 1920 x 1080 XRGB8888 frame visible to this non-coherent device.

Commit `964e47f6e` exposes the existing EIC7700 Die 0 cache-clean operation
through OSTD's DMA module and adds an opt-in probe. It runs only when both
`asterinas.dc_probe=1` and `asterinas.dc_dma_probe=1` are present. The probe
cleans one 8,294,400-byte span at the beginning of the contiguous GEM pool in
256 KiB chunks. It does not write display-controller registers, switch the
active scanout, or change the default DRM path.

The span is allocated and retained by the VMO. This cache operation alone does
not establish a DMA mapping, prove that the controller can read the address,
or resolve simultaneous userspace writes. Those require a separate hardware
pixel and flip experiment before enabling native scanout.

## Controlled physical result

The RISC-V release kernel built with
`make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode RELEASE=1`.
The selected image was
`asterinas-dma-probe-964e47f6e-4e9a4ef3.booti`, SHA-256
`4e9a4ef3231eda3491ae7262100917b07c8ba6e58bce86ae5e47b8667095eb38`.
The prepared DTB and stage-1 image were retained from the passing prior boot.
Their SHA-256 values were
`465cb129333cc334a3161fabb43d37df8738ac4baa86fcdb50260691a22a84ba`
and `ea446515661f2380568426b3c37da0b6a0698fc51f88d32c62807fce04890962`.
The three files were staged under
`/home/debian/asterinas/dma-probe-964e47f6e/` on RockOS partition 3;
local and remote sizes and SHA-256 hashes matched. U-Boot also checked each
loaded byte count and CRC32 before one temporary `booti`.
The host used
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0` as the sole
serial owner. U-Boot loaded the kernel at `0x80200000`, the DTB at
`0xf0000000`, and stage 1 at `0x83000000`, then ran
`booti 0x80200000 0x83000000:${initrd_size} 0xf0000000` with the two probe
flags in the candidate boot arguments.

The previous Asterinas root console reported boot ID
`beced7f9-af44-4381-8c51-3afae824af6a` and rebooted by software into the
normal RockOS login. The selected candidate boot ID was
`6809f8c6-e206-4291-be3d-170219ad477b`. Its root debug console returned
UID 0 and the same boot ID after the host closed and reopened the stable serial
device. Xorg reached 1920 x 1080; the Firefox service reported `MainPID=129`,
`NRestarts=0`, and `SubState=running`. Only then was the recovery watchdog
disarmed; its final value was `0`. The boot entry is temporary, so an ordinary
reboot still selects RockOS.

The kernel logged:

```text
ASTERINAS_DC_PROBE primary_addr=0xfd800000 stride=7680 config=0x18004011 display_h=0x8980780 display_v=0x4650438 pool_addr=0xf8000000 pool_size=67108864
ASTERINAS_DC_DMA_PROBE clean_bytes=8294400 clean_ns=6746000 pool_addr=0xf8000000
```

The 6.746 ms cache-clean observation is materially below the previously
measured 78.627 ms full-frame firmware copy, but they are different operations
and different selected boots. Cache cleaning does not include display-register
submission, vblank wait, actual DMA read, or visible-frame latency. No Firefox
speedup is established by this probe.

## Decision for P1

The result supports a narrowly guarded direct-scanout experiment using the
existing physically contiguous VMO, with explicit cache cleaning before each
display handoff. Before any display-register write, validate the exact buffer
address and length against the controller's address width and the running
mode, and retain the pool while the controller may read it. The first hardware
gate must verify actual pixels and recovery from a failed flip. It must also
check the shadow-register and vblank behavior on this firmware handoff; the
live `config` value has shadow bit 3 clear. Do not claim tear-free flips or
deliver completion events until the vblank and buffer-lifetime path works.

Raw local evidence is in
`/home/ubuntu/.codex/asterinas-dma-probe-20260926/`:
`manifest.json`, `candidate-boot.result.json`, the serial log, and
`dma-probe-sample.txt`.
