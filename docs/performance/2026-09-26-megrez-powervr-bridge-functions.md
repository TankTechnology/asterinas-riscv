# PowerVR functions observed in the RockOS 16 × 16 GLES test

This is the **observed client surface**, not a complete driver or a list of
operations Asterinas already implements. It maps the
[188-call trace](2026-09-26-megrez-powervr-bridge-inventory.json) to the
generated bridge command IDs at RockOS kernel commit
`bf2ec5d53002c16bc1bc593b92516eb6c2866176` (DDK 24.2@6643903).
The group definitions are in the vendor
[`pvr_bridge.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/include/pvr_bridge.h)
and [`rgx_bridge.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/include/rgx_bridge.h).
The command names come from the matching generated
[`srvcore`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/generated/volcanic/srvcore_bridge/common_srvcore_bridge.h),
[`sync`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/generated/volcanic/sync_bridge/common_sync_bridge.h),
[`mm`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/generated/volcanic/mm_bridge/common_mm_bridge.h),
[`cache`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/generated/volcanic/cache_bridge/common_cache_bridge.h),
[`rgxta3d`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/generated/volcanic/rgxta3d_bridge/common_rgxta3d_bridge.h),
and [`rgxtq2`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/generated/volcanic/rgxtq2_bridge/common_rgxtq2_bridge.h)
headers.

| Group | Function | Vendor command | Calls |
| --- | ---: | --- | ---: |
| 1 services | 0 | `CONNECT` | 1 |
| 1 services | 2 | `ACQUIREGLOBALEVENTOBJECT` | 1 |
| 1 services | 4 | `EVENTOBJECTOPEN` | 5 |
| 1 services | 10 | `ALIGNMENTCHECK` | 1 |
| 1 services | 15 | `ACQUIREINFOPAGE` | 1 |
| 2 sync | 0 | `ALLOCSYNCPRIMITIVEBLOCK` | 2 |
| 2 sync | 2 | `SYNCPRIMSET` | 3 |
| 2 sync | 7 | `SYNCALLOCEVENT` | 3 |
| 6 memory | 3 | `PMRMAKELOCALIMPORTHANDLE` | 3 |
| 6 memory | 4 | `PMRUNMAKELOCALIMPORTHANDLE` | 3 |
| 6 memory | 6 | `PMRLOCALIMPORTPMR` | 4 |
| 6 memory | 8 | `PHYSMEMNEWRAMBACKEDPMR` | 57 |
| 6 memory | 9 | `DEVMEMINTCTXCREATE` | 1 |
| 6 memory | 11 | `DEVMEMINTHEAPCREATE` | 14 |
| 6 memory | 16 | `DEVMEMINTRESERVERANGEANDMAPPMR` | 57 |
| 6 memory | 18 | `CHANGESPARSEMEM` | 6 |
| 6 memory | 22 | `HEAPCFGHEAPCOUNT` | 1 |
| 6 memory | 24 | `HEAPCFGHEAPDETAILS` | 14 |
| 13 cache | 0 | `CACHEOPQUEUE` | 2 |
| 130 TA/3D | 10 | `RGXKICKTA3D2` | 1 |
| 130 TA/3D | 13 | `RGXCREATEFREELIST` | 2 |
| 130 TA/3D | 14 | `RGXCREATERENDERCONTEXT` | 1 |
| 130 TA/3D | 15 | `RGXCREATEHWRTDATASET2` | 1 |
| 137 transfer | 0 | `RGXTDMCREATETRANSFERCONTEXT` | 1 |
| 137 transfer | 4 | `RGXTDMSUBMITTRANSFER2` | 2 |
| 137 transfer | 5 | `RGXTDMGETSHAREDMEMORY` | 1 |

Memory management accounts for 160 of the 188 observed bridge calls. The
client creates GPU memory contexts, heaps, and mappings before its single
TA/3D kick. This makes DMA allocation, GPU address translation, and cache
visibility the first functional implementation boundary. Even if a trusted
service handles these vendor command IDs, the RockOS driver initializes the
GPU, firmware, and GPU MMU *before* it dispatches them. Replaying this table
against an uninitialized device cannot draw pixels.

The trace records the outer ioctl result only; it does not read each generated
output struct's `eError` field. It also ends after the pixel readback without
showing a complete teardown sequence. The first service prototype must capture
bridge-level status and fd-close cleanup before this list is treated as a
minimal supported ABI.

The tracer now has a separate `ASTERINAS_IOCTLTRACE_PVR_STATUS=1` opt-in for
the next matching RockOS reference run. It uses the packed output structs'
checked `eError` offsets and a fault-safe self-process read. Unknown command
IDs, short outputs, and unreadable pointers yield `status=unavailable`, never a
guessed success. This new mode has passed host checks for known offsets and an
unreadable page, but no new physical RockOS trace has been collected yet.
