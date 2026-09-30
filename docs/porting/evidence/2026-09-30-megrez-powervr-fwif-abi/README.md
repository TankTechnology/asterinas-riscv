# Megrez PowerVR FWIF ABI extraction (2026-09-30)

The selected RockOS driver commit is
[`bf2ec5d53002c16bc1bc593b92516eb6c2866176`](https://github.com/rockos-riscv/rockos-kernel/tree/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic).
Its `config_kernel.h` SHA-256 is
`b81363e8593062b1f5a4cb808a08eed204030f3cbec058a270c2c76bdcfc265d`;
it selects `PVRSRV_HWPERF_COUNTERS_PERBLK=12` and `SUPPORT_SOC_TIMER`.
The pinned `include/volcanic/rgx_fwif_km.h` SHA-256 is
`e897fd683cec7feb5ca3908f1d9a9cf60a342111d3820ce304f7d490bb3f58c2`.

[`rgx_fwif_abi_probe.c`](../../../../tools/riscv/drm/rgx_fwif_abi_probe.c)
uses the vendor's actual types and `offsetof`, rather than a hand-written
structure mirror. The
[`measure_rgx_fwif_abi.sh`](../../../../tools/riscv/drm/measure_rgx_fwif_abi.sh)
tool compiles it with both `riscv64-linux-gnu-gcc` and native GCC, extracts
the named ELF section, and fails if the sections differ byte for byte. The
cross compiler used the explicitly isolated
[probe-only libc stubs](../../../../tools/riscv/drm/rgx_fwif_abi_stubs/README.md)
because the development container lacks RISC-V libc headers. Both compilers
produced section SHA-256
`f02770440039f98ef331d30317073f931fdfaefff52d3d461ce9cdafd89b5ae3`.
The decoded [layout](layout.json) is the input for the next FWIF allocation
and initialization change.

The most relevant fields are:

| Object | Size | Required observations |
| --- | ---: | --- |
| `RGXFWIF_CONNECTION_CTL` | 16 B | OS connection state at offset 4 |
| `RGXFWIF_OSINIT` | 112 B | kernel/FW CCB pointers at 0–16; HWR buffer pointer 28; OS data pointer 36 |
| `RGXFWIF_SYSINIT` | 440 B | runtime, trace, system-data pointers at 160–168; `bFirmwareStarted` at 208; coremem DMA address at 224 |
| FW virtual pointer | 4 B | distinct from an 8 B GPU/physical address |

The [vendor OS initialization path](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxfwutils.c)
allocates HWR info (2,464 B), kernel and firmware CCB controls (16 B each),
64 B command entries, return slots, OS data (1,096 B), and a power sync
primitive before copying OSINIT. Its system path allocates trace control
(864 B), system data (3,656 B), GPU utilization data (11,824 B), runtime
configuration (184 B), and other dependent objects before copying SYSINIT.
The 12,296 B register configuration allocation is conditional on
`SUPPORT_USER_REGISTER_CONFIGURATION`, which this selected `config_kernel.h`
does not enable. The pointer graph is therefore substantially
larger than the three currently mapped, zeroed configuration slots.

For reproduction, extract the pinned driver's `include` and
`hwdefs/volcanic` directories, plus `config_kernel.h`, without modifying
their contents. Inside the persistent Asterinas development container, run:

```sh
sh tools/riscv/drm/measure_rgx_fwif_abi.sh \
  /path/to/img-volcanic /path/to/config_kernel.h /path/to/output
cmp /path/to/output/layout.json \
  docs/porting/evidence/2026-09-30-megrez-powervr-fwif-abi/layout.json
```

This extraction verifies structure sizes and offsets for this selected
configuration. It does **not** initialize the FWIF, prove every conditional
allocation has been captured, start firmware, or demonstrate GPU rendering.
Before releasing META reset, the kernel still needs owned GPU mappings for
every enabled dependent object, valid firmware addresses and initial values,
MMU-root installation, and a bounded start/fault/recovery sequence. A firmware
ready flag alone is not a safe or sufficient success check.

## Owned field access in the prepared GPU MMU

The first kernel use of this layout adds bounded 32-bit CPU reads and writes
through the `GpuMmu4` allocation owner. A field must be aligned and wholly
inside an owned mapping. The GPU page-table read-only bit does not prohibit
CPU writes through the owner's uncached DMA alias: the vendor's
`RGX_FWSHAREDMEM_GPU_RO_ALLOCFLAGS` explicitly includes `CPU_WRITEABLE`. This is
needed to initialize FWIF fields and later poll `bFirmwareStarted` without
losing the uncached CPU alias when the allocation moves into the GPU MMU.
The [focused RISC-V QEMU ktest](mmu-owned-field-ktest.txt) exercised the
vendor-derived offset 208, a mapping boundary, a misaligned read, and a
GPU-read-only mapping: `1 passed; 0 failed`. A follow-up test corrected the
CPU/GPU permission distinction and passed with `1 passed; 0 failed`.
The ordinary RISC-V kernel build and
targeted rustfmt check also passed. The new accessor has **not** yet been
tested on the board or used to start firmware.

## META firmware address encoding

The pinned driver's
[`RGXSetFirmwareAddress`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxfwutils.c)
subtracts the raw firmware-heap base from a GPU VA, then adds the META data
base and cache bits defined by
[`rgx_meta.h`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/include/rgx_meta.h).
The result is a 32-bit firmware pointer, **not** the low 32 bits of the GPU VA.
The driver's
[`rgxfwutils.h` allocation flags](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxfwutils.h)
select GPU uncached for all three config allocations, but FIRMWARE_CACHED
for OSINIT and SYSINIT only. Thus their selected encoded addresses are:

| Config object | GPU VA | META firmware address |
| --- | ---: | ---: |
| Connection control | `0xe1c1fd0000` | `0xf1fd0000` |
| OSINIT | `0xe1c1fe0000` | `0x71fe0000` |
| SYSINIT | `0xe1c1ff0000` | `0x71ff0000` |

The new kernel encoder checks alignment and the 32 MiB raw-heap bounds; the
staging path computes and logs these addresses while still reporting
`fw_config_initialized=0` and `gpu_root_installed=0`. The two
[focused RISC-V QEMU tests](fwif-address-ktest.txt) each passed with zero
failures. The ordinary RISC-V kernel build and targeted formatting check
also passed. This version of the encoder has **not** run on the board.

The GPU PTE still has `AXCACHE_WBRWALLOC`: the selected driver's
[`RGXDerivePTEProt8`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/rgxmmuinit.c)
sets that fabric-level field even for GPU-uncached allocations. The META
pointer cache bits and PTE AXCACHE field describe different parts of the
access path; their differing values are not by themselves evidence of a
mapping error or of cache coherence.

## First owned SYSINIT objects

The staging path now allocates four zeroed firmware-main objects in Die 0 DMA
memory, maps them with one unmapped guard page after each object, and writes
their encoded META addresses into the selected SYSINIT slot:

| Object | Bytes | GPU VA | SYSINIT offset | META address | GPU PTE |
| --- | ---: | ---: | ---: | ---: | --- |
| Trace control | 864 | `0xe1c0040000` | 164 | `0xf0040000` | read/write |
| System data | 3,656 | `0xe1c0042000` | 168 | `0xf0042000` | read/write |
| GPU utility | 11,824 | `0xe1c0044000` | 172 | `0x70044000` | read/write |
| Runtime config | 184 | `0xe1c0048000` | 160 | `0xf0048000` | read-only |

SYSINIT starts with `bFirmwareStarted=0` at offset 208 and marker 1 at
offset 212. The GPU utility object uses the vendor's firmware-cached flag;
the other three use uncached firmware addresses. The focused
[QEMU kernel results](fwif-sysinit-links-ktest.txt) check the four pointers,
PTE permissions, guard pages and unchanged config/firmware segment mappings.
The ordinary RISC-V Sv39/SMP=4 kernel build and targeted rustfmt check also
passed. The existing `fw_config_initialized=0` log still means the complete
FWIF graph has not been initialized. The GPU page-table root is still not
installed. Other required SYSINIT and
OSINIT dependencies remain uninitialized; this change does **not** start the
firmware, submit GPU commands, or prove hardware drawing.

## Owned OSINIT control queues

The next staging step maps OSINIT's HWR buffer, kernel CCB control and command
ring, kernel return slots, firmware CCB control and command ring, and OS data.
The selected `config_kernel.h` sets `PVRSRV_APPHINT_KCCB_SIZE_LOG2=10`; the
selected `rgxfwutils.c` sets firmware CCB log2 to 5 without
`SUPPORT_PDVFS` or `SUPPORT_WORKLOAD_ESTIMATION`. The ring capacities are
therefore 1,024 and 32 commands. Both control blocks have their wrap masks
initialized at offset 8, respectively 1,023 and 31.

| OSINIT field offset | Object | GPU VA | META address | GPU access |
| ---: | --- | ---: | ---: | --- |
| 28 | HWR info, 2,464 B | `0xe1c0050000` | `0xf0050000` | read/write |
| 0 | Kernel CCB control, 16 B | `0xe1c0052000` | `0xf0052000` | read/write |
| 4 | Kernel CCB, 65,536 B | `0xe1c0054000` | `0x70054000` | read-only |
| 8 | Kernel return slots, 4,096 B | `0xe1c0065000` | `0xf0065000` | read/write |
| 12 | Firmware CCB control, 16 B | `0xe1c0067000` | `0xf0067000` | read/write |
| 16 | Firmware CCB, 2,048 B | `0xe1c0069000` | `0xf0069000` | read/write |
| 36 | OS data, 1,096 B | `0xe1c006b000` | `0xf006b000` | read/write |

The OS data's `sPowerSync` field at offset 572 points to a separate zeroed
firmware-main DMA page at GPU VA `0xe1c006d000`, encoded as `0xf006d000`.
The pinned driver obtains this address through
[`SyncPrimGetFirmwareAddr`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/shared/common/sync.c)
after its
[`RGXAllocUFOBlock`](https://github.com/rockos-riscv/rockos-kernel/blob/bf2ec5d53002c16bc1bc593b92516eb6c2866176/drivers/gpu/drm/img/img-volcanic/services/server/devices/volcanic/rgxinit.c)
allocates from FW_MAIN and calls `RGXSetFirmwareAddress`. Each object has an
unmapped guard page. The [focused QEMU tests](fwif-osinit-ccb-ktest.txt)
verify OSINIT pointers, ring masks, PTE permissions, power-sync pointer and
the earlier staging layout: four selected tests passed. The normal RISC-V
Sv39/SMP=4 kernel build and targeted rustfmt check passed. This code has not
been booted on Megrez; it does not install the GPU MMU root, start firmware,
submit commands or provide a fence/pixel result. The remaining SYSINIT fields,
device registers and fault handling must be established before a start attempt.
