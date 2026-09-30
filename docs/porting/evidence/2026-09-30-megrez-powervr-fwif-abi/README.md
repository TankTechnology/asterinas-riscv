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
configuration (184 B), register configuration (12,296 B), and other dependent
objects before copying SYSINIT. The pointer graph is therefore substantially
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
inside an owned mapping; writes to read-only mappings are rejected. This is
needed to initialize FWIF fields and later poll `bFirmwareStarted` without
losing the uncached CPU alias when the allocation moves into the GPU MMU.
The [focused RISC-V QEMU ktest](mmu-owned-field-ktest.txt) exercised the
vendor-derived offset 208, a mapping boundary, a misaligned read, and a
read-only mapping: `1 passed; 0 failed`. The ordinary RISC-V kernel build and
targeted rustfmt check also passed. The new accessor has **not** yet been
tested on the board or used to start firmware.
