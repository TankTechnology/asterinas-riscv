# EIC7700 DWC3 QEMU Phase 2: deterministic USB transfer

Date: 2026-10-04

## Scope and artifacts

Phase 2 extends the Phase 1 EIC7700 DWC3 model with a deterministic USB bulk
loopback device and an opt-in Asterinas guest self-test. The QEMU source is in
`/home/ubuntu/.codex/qemu-eic7700-10.2.1` on branch
`codex/eic7700-usb-data-phase2` at commit
`9c361e02b0527d41e21b2e0a73e86df36abbc6a8`.

The QEMU device has USB ID `0627:7700`, one vendor interface and endpoint 1 in
both directions. It stores one bulk OUT payload and returns it from bulk IN.
Vendor request `0x5a` returns reset, transfer and byte counters. Asterinas only
runs this test when `asterinas.usb_bulk_selftest` is present, so normal DWC3 HID
startup is unchanged.

The raw logs, DTB and structured summary are under the ignored local directory
`target/eic7700-qemu/phase2/`. The relevant hashes are:

- QEMU binary: `93f68c59fa5c47be8b39fd44191a2514f6465e668564117c86fadf8723d72827`
- Asterinas kernel: `9c4546c2a4b928b2e7851fee97d0e29b66a8e773c707f0c33c2966be20277f48`
- initramfs: `7cd5ce87b456ff6add1be32bf877cbdc3b7069962f31ab5463d1899663ef112c`
- generated DTB: `3f3adc3d1e18668208f904fe0e26597a969f7e7d356ab784bc6df2e8cf75f41c`

## Verification

The QEMU qtest suite passed all five tests, including deterministic device
hotplug, event-ring DMA and an asserted controller IRQ:

```text
ok 1 /riscv64/eic7700/dwc3/identity-isolation
ok 2 /riscv64/eic7700/dwc3/glue-system-reset
ok 3 /riscv64/eic7700/dwc3/event-dma-plic-irq
ok 4 /riscv64/eic7700/dwc3/irq-assert-deassert
ok 5 /riscv64/eic7700/dwc3/bulk-loopback-hotplug
```

The Asterinas parser and stats contract was run as a RISC-V kernel test. The
OSDK runner currently chooses `q35` incorrectly for this invocation, so the
built ktest ELF was booted directly with the RISC-V `virt` machine:

```text
test ostd::bus::usb::tests::parses_exact_bulk_loopback_statistics ... ok
test result: ok. 1 passed; 0 failed; 310 filtered out.
```

The complete RISC-V kernel build also passed:

```text
tools/docker/run_dev_container.sh -- make kernel \
  TARGET_ARCH=riscv64 SMP=1 FEATURES=riscv_sv39_mode
```

Three independent, bounded guest boots produced byte-for-byte identical result
fields:

```text
ASTERINAS_USB_BULK_SELFTEST result=pass controller=0 \
payload_bytes=1021 requested_in_bytes=1088 \
sha256=c30b5b3bb192d370a1f07db31d6a39d186b0fbd24caa5e3187e60f85be8573ff \
resets=2 out_transfers=1 in_transfers=1 out_bytes=1021 in_bytes=1021
```

The 1088-byte IN request completing with 1021 bytes verifies short-packet
handling. Each run observed 12 transfer completion events, 5 command events, 1
port event and 16 event-handler passes. The qtest hotplug case observed one IRQ
assertion; the Asterinas guest intentionally polls and disables delivery of the
external xHCI interrupt during this self-test.

Every boot was terminated by `timeout` inside the builder container. A process
inventory after the repeated run found no remaining QEMU process. `git
diff --check` passed in both repositories. QEMU `checkpatch.pl` reported no
errors and one MAINTAINERS warning for the new device file.

## Boundaries

This proves control transfers, endpoint configuration, bulk OUT, bulk IN,
short-packet completion, event-ring DMA and the model's reset/counter protocol
through the EIC7700 DWC3 wrapper. It does not model a physical USB PHY, timing,
isochronous traffic or the EIC7700 board's cache-coherency behavior. Those
remain outside Phase 2 and must not be presented as real-board validation.
