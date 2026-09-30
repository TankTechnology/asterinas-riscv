# Megrez META boot address binding and utilization initialization

This change validates the selected META boot configuration against the live
session's owned firmware mappings and initializes native GPU-utilization time
fields. It does not add a firmware release/start request or enable rendering.
GPU page walks, firmware execution, command completion and pixel readback remain
unverified by these CPU/QEMU checks.

The previous physical reset/catalogue preflight already used `rgx_meta_boot.py`
and its prepared manifest. The client's default manifest instead describes raw
LDR materialization. The missing piece was kernel-side verification of the
prepared code against the addresses actually owned by the MMU session.

## Configuration reference and owner checks

A read-only RockOS serial command retrieved `/lib/firmware/rgx.fw.30.3.408.101`.
Its 126,976 bytes matched the pinned SHA-256
`25e9e7ff4645292ceb991617dba028e350a5c17088a105230dbaaaf995f4413b` on the
board and again on the host. The existing builder generated a 296-byte boot
configuration at code offset 512: five privilege/segment pairs, 17 LDR register
writes, 12 cache pairs, a terminator and the coremem footer. Configuration
SHA-256 is `2c70442a16a343dbae1312d3d1f54fedf773a26d544ff4702c58cb8fcd88d4d5`.
The generated [manifest](prepared-manifest.json) records all segment hashes.
The firmware itself remains only in the untracked local `target/` directory.

The checked-in `meta_boot_config.bin` contains that register/configuration
reference, not executable firmware. The kernel derives the data segment's GPU
address and coremem's META/GPU pointers from the actual MMU owner's four segment
addresses, then checks every configuration word through bounded uncached CPU
aliases. It rejects incomplete staging, a wrong boot-code base, invalid firmware
addresses, unprepared code and any altered configuration word. A pointer into a
neighbouring page of an owned segment is still rejected because it is not the
segment base expected by the bootloader. This validates boot configuration;
the root-only staging client remains responsible for checking image/segment
identity before opening the control device.

The selected opt-in flag is `asterinas.powervr_boot_config_preflight=1`.
Validation runs before the selected META reset and catalogue setup. Its marker
is `ASTERINAS_POWERVR_META_BOOT status=cpu_config_checked`, with GPU visibility
explicitly unverified. The default raw staging/preflight path remains available.

## Explicit release gate

The control endpoint now accepts the exact four-byte command `PVRR`, but only
when the boot command line also contains `asterinas.powervr_release=1`. The
release path takes the staging lock before checking its one-shot flag, verifies
the prepared boot bytes again, prepares the selected META reset sequence,
installs context 0's catalogue, and performs a read-only catalogue validation
before the first reset-release write. It then waits at most one second (and
1,000 bounded observations) for the native started flag, rejecting firmware
fault and HWR recovery indications. A failed attempt leaves the owner in the
reset-before-DMA cleanup path; a successful attempt reports
`gpu_visibility=observed` but still does not imply command execution or pixels.

The board run on 2026-09-30 did not reach this command: its first attempt tried
to download the staging scripts with `curl`, but the selected Asterinas boot
has no network service, so the command waited until the 300-second software
reboot returned the board to RockOS. The next physical run must inject the
scripts offline (serial transfer or initramfs) and use a longer bounded boot
window. No firmware release, GPU execution, or pixel result is claimed.

The offline initramfs builder now packages the four checked segments and a
static RISC-V init that sends the frames and `PVRR` directly. It requires the
generated prepared manifest, so a stale LDR scan cannot accidentally bypass
the boot-config check:

```sh
tools/docker/run_dev_container.sh -- python3 tools/riscv/drm/powervr_release_gate.py \
  --segments-dir target/powervr-boot-config/prepared \
  --manifest target/powervr-boot-config/prepared/manifest.json \
  --output target/powervr-boot-config/board-preflight/powervr-release-initramfs.cpio.gz
```

That artifact is ready for the next serial-loaded board run. Its success
marker is `PVR_RELEASE_PASS`; a failure prints `PVR_RELEASE_FAIL` and leaves
the kernel watchdog responsible for returning to RockOS.

The 2026-10-01 host-runner attempt stopped before U-Boot: the board was at a
RockOS login prompt, so the unauthenticated reboot request was rejected. The
runner was interrupted before any artifact upload; its retained serial log is
`target/powervr-release-physical-20261001/serial.log`. This is a control-channel
precondition failure, not a GPU release result.

```sh
python3 rgx_meta_boot.py /lib/firmware/rgx.fw.30.3.408.101 \
  --output-dir /run/pvr-prepared
python3 powervr_dma_stage.py --segments-dir /run/pvr-prepared \
  --manifest /run/pvr-prepared/manifest.json --check-boot-config --status
```

`--check-boot-config` requires the corresponding selected kernel flag, the
prepared manifest's exact layout/options, and the pinned configuration digest
before any device open. It rejects raw manifests, drifted addresses and changed
configuration bytes even if the whole segment's manifest digest was updated.

## Native utilization initialization

The ABI probe now extracts 115 entries. RISC-V and native ELF sections matched
byte for byte; [layout.json](layout.json) has SHA-256 for its binary source
`7c683534f712fd034a4a0c61d5f4baaf020cac945e16f881114ce093aed07655`.
The alignment-check binary remains
`59ecbcd04bf00585bbb0bc17e8b8561cc21169b5c5c73fb18f4f1000a911c682`.

| Field/constant | Measured value |
| --- | ---: |
| GPU_UTIL_FW.ui64GpuLastWord | 10248 |
| GPU_UTIL_FW.sStats | 10288 |
| GPU_STATS size | 192 |
| GPU_STATS.aui32DMOSLastWord / Wrap | 0 / 32 |
| Native drivers / DM count | 1 / 8 |
| IDLE / state mask | 0 / 3 |
| DM OS timestamp shift | 10 |

After reading the actual powered core clock, staging initializes the global
last word from monotonic nanoseconds with IDLE state in the low two bits. For
native driver 0, all eight DM low/high timestamp words use nanoseconds shifted
right by ten. Other driver slots and counters stay zero. Each write uses owned,
bounded mappings and CPU readback. This is initialization data, not a measured
GPU utilization result or proof of device coherence.

The [negative tests](negative-tests.txt) record real RISC-V QEMU failures for the
missing configuration check and the missing time initialization. Both focused
tests subsequently executed one test and passed; see [positive tests](positive-tests.txt).
The configuration test checks raw code, exact prepared words, data/coremem
address drift and altered LDR data. The time test checks global low/high words,
all eight DM low/high words, untouched non-native slots and zero timestamps.
Eight Python tests passed for the staging client and existing META builder.

The next firmware-start gate still needs the remaining selected SYS/OS config
and HWPerf metadata audit, a hardware adapter for bounded startup observation,
and actual release/fault/recovery evidence. No Task 3 completion is claimed.
