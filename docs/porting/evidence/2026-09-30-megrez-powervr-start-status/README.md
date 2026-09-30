# Megrez PowerVR META release and status preparation (2026-09-30)

This checkpoint prepares the next physical firmware experiment. No board boot,
META release, GPU page walk, firmware execution, or hardware pixel was performed
for this change. The existing device write path still only stages firmware and
runs explicitly selected reset/catalogue preflights; it does not call the new
release operation.

The selected RockOS `bf2ec5d5` `rgxstartstop.c::DeassertMetaReset` waits at least
32 GPU cycles before and after writing `SOFT_RESET=0`, and reads that register
after the write. The new kernel operation checks that GARTEN is held and META
master boot is selected, applies that sequence, and checks the release readback.
The hardware implementation reuses the existing 15-microsecond reset delay,
which exceeds 32 cycles at the selected 800 MHz core clock. Its owner-provided
`meta_release_attempted` flag is set before the release write, so a write or
readback error must use the reset-before-DMA-release cleanup path introduced in
`56685a37b`.

The regression run with that flag store removed failed on the assertion that
the attempt was recorded after a deliberately mismatched readback. The selected
QEMU process eventually exited with status 1; it was not killed or restarted.
Restoring the store made the same test pass. The [negative excerpt](meta-marker-red-ktest.txt)
and [positive results](ktest-results.txt) preserve this distinction. Separate
RISC-V QEMU tests passed for release ordering, rejection of unprepared/reset-mode
drift, the original held-META prefix, and bounded DMA status access. Each selected
positive run executed one test and reported `1 passed; 0 failed`.

## Diagnostic memory status

The [FWIF ABI probe](../../../../tools/riscv/drm/rgx_fwif_abi_probe.c) now measures
seven additional fields. RISC-V and native compiler sections matched byte for
byte, with SHA-256
`2d65ab9fed175fe799999bab7fc8ed14ddafd5beba656ffec5b17cd7b3045530`.
The alignment-check section remains unchanged. In the selected layout:

| Object | Field | Offset |
| --- | --- | ---: |
| SYSDATA | firmware faults | 3536 |
| SYSDATA | HWR state flags | 3608 |
| HWRINFOBUF | HWR count | 2304 |
| COMPCHECKS inside OSINIT at 40 | updated | 56 |
| COMPCHECKS | DDK version / build / options | 32 / 36 / 40 |

The DMA stage retains the actual owned SYSDATA and HWRINFOBUF GPU addresses.
Status reads go through its bounded uncached CPU aliases, reject incomplete
staging, and perform no GPU-register access. `/dev/powervr-control` exposes these
observations only after the existing capability check. A read requires at least
48 bytes and returns `PVS1` followed by eleven little-endian 32-bit words:

1. META release attempted
2. firmware started
3. firmware-start timestamp
4. firmware faults
5. HWR state flags
6. HWR count
7. compatibility information updated
8. DDK version
9. DDK build
10. build options
11. firmware connection state

These asynchronously updated fields are diagnostic observations, not an atomic
snapshot or a ready verdict. Native firmware may not use the virtualization
connection state as a readiness signal. Faults remain visible even if the started
flag is set. The QEMU test injected nonzero fault/HWR values through owned
mappings and checked that they were returned, alongside timestamp and build
fields. It does not establish GPU-side visibility.

The staging client supports `--status` to save a JSON line before closing the
same session; final close still restores the GPU owner. Its four host tests passed,
including rejection of truncated or unknown-version status frames.

```sh
python3 tools/riscv/drm/powervr_dma_stage.py \
  --segments-dir /path/to/checked/segments --manifest /path/to/manifest.json \
  --status
```

Run the selected kernel tests in the persistent development container with:

```sh
cd kernel
cargo osdk test selected_meta_release_readback_failure_requires_running_cleanup \
  --target-arch riscv64 --scheme riscv --features riscv_sv39_mode \
  --initramfs /root/asterinas/test/initramfs/build/initramfs.cpio.gz
```

The other exact test names are recorded in `ktest-results.txt`. Each OSDK run
overwrites `qemu-serial.log`, so retained copies are under `target/` with the
test's name or the `powervr-status`/`powervr-meta-marker` prefix. Before physical
release, the remaining selected FWIF initialization audit, bounded wait/fault
handling, explicit startup request, and RockOS recovery check must still be
completed. No render node or Firefox acceleration is enabled by this change.

## Management-channel check

Before this checkpoint, two nonce-framed read-only commands, across a serial
close/reopen, returned UID 0 and the same RockOS Linux boot identity
`2e6d0321-293a-4c67-810f-f21a3b8392fc`. The stable serial device was
`/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_AL02XYO2-if00-port0`.
The retained host log is `target/powervr-start-status-control.serial.log`,
SHA-256 `dbf4a002acb16ae3aba0f12422c01e55d1c8764398a5bc1c1c4d86fd3ba7d2fe`.
This proves that control was available after reopening; no reboot persistence
or new physical GPU validation was exercised by this checkpoint.
