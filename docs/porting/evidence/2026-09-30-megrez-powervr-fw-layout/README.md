# Megrez PowerVR firmware layout preflight (2026-09-30)

The selected Asterinas desktop boot has the exact BVNC-matched PowerVR
firmware staged locally, but it has **not** started the GPU firmware. A
read-only preflight on that board parsed the vendor version-2 footer and
confirmed that this firmware uses the **META** processor layout. The RISC-V
architecture of the host CPU does not imply a RISC-V GPU firmware layout.

The firmware file `/lib/firmware/rgx.fw.30.3.408.101` was 126,976 bytes with
SHA-256 `25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b`.
Its footer reported BVNC `30.3.408.101`, DDK `24.2.6643903`, format version
2, four 24-byte entries, flags `0x80020810`, and four META section IDs. The
allocation sizes reported by the image were:

| Segment | Allocation bytes | Maximum section bytes |
| --- | ---: | ---: |
| Code | 52,064 | 266,240 |
| Data | 18,432 | 90,112 |
| Coremem code | 73,312 | 73,312 |
| Coremem data | 9,984 | 9,984 |

The sizes sum to 153,792 bytes before per-allocation alignment. This is a
firmware-layout fact, not a measured GPU-visible DMA requirement. The pinned
RockOS DDK's `RGXProcessFWImage` takes the META path and processes an LDR
command stream for this kind of firmware; copying the raw blob into a buffer
would not be a valid firmware startup. The next implementation must decode
that stream with bounds checks, prepare the code/data allocations and boot
configuration, establish GPU MMU mappings and cache visibility, then perform
a bounded handshake and fault check.
An additional read-only check found the LDR stream pointer `0x1190` in the
first 16-byte header and command `0x5` (CONFIG) at that address. The complete
LDR chain was then bounds-checked by
[`rgx_meta_ldr.py`](../../../../tools/riscv/drm/rgx_meta_ldr.py): 56 linked
blocks contained 41 LOADMEM, 9 ZEROMEM, 2 CONFIG, 1 START_THREADS, and 3
comment commands. The CONFIG blocks requested 17 boot-configuration register
writes. The checked write lengths were 45,848 code, 10,220 data, 62,040
coremem code, and 796 coremem data bytes. The vendor's coremem-data
ZEROMEM skip applied to 7,418 bytes; the scanner restricts that skip to the
image-declared coremem span rather than accepting arbitrary addresses.
[`ldr-scan.json`](ldr-scan.json) contains this metadata. The scanner's host
and guest SHA-256 both matched
`5902a287d3d97f7cacb538bb7763b8768d7ab29ee3b69ac8e691fb064f1a21c5`.
No LDR command was executed, no firmware buffers were materialized, and no
GPU address translation or firmware handshake was exercised.

[`layout.json`](layout.json) contains only metadata and a digest; licensed
firmware bytes are not stored in Git. The parser is
[`rgx_firmware_layout.py`](../../../../tools/riscv/drm/rgx_firmware_layout.py).
Its synthetic contract tests reject wrong BVNC, malformed table shape,
duplicate sections, and invalid allocation ranges. The script's host and
guest SHA-256 both matched
`27583f4ca642daa903ec00590661a1dccdf8f4f1f70e2e45eaec4b37a6a9c7a9`.
It was fetched into `/run` and invoked with the staged firmware path; the
firmware binary itself was never uploaded to the host. The board run used a
nonce-framed UID-0 serial command on
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`, with boot ID
`d5e68c9c-bd7e-452d-b5d9-56c73a1574b4`. A later fresh serial connection
again returned UID 0 and the same boot ID. The current desktop boot has no
new PowerVR render node, and this preflight did not alter its boot selection.

The format and META LDR path were cross-checked against the pinned
[RockOS firmware header](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/include/rgx_fw_info.h)
and [RockOS image processing implementation](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxfwimageutils.c).
