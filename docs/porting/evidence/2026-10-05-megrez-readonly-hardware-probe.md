# Megrez read-only hardware Probe, 2026-10-05

The machine-readable completion audit is retained at
`docs/porting/evidence/2026-10-05-megrez-probe-completion-audit.json`.

The candidate selector was published as a canary only. The persistent
`asterinas.conf` selector was not replaced. Every cycle used the hardware
watchdog and returned through OpenSBI/U-Boot or the RockOS fallback.

## Implementation and checks

- The selector is parsed once by `aster-cmdline` as
  `asterinas.hardware_probe=<device>`. USB and GMAC both observe it and skip
  their normal side-effecting initializers when another device owns the boot.
- USB reads only offsets `0x0010`, `0xc110`, and `0xc120`.
- GMAC reads only `0x0000`, `0x00b0`, `0x0110`, `0x0120`, `0x1000`, and `0x1008`.
  It does not program clocks, reset, GPIO, PHY/MDIO, DMA, or IRQs.
- Physical device-probe cycles now reject a missing `--hardware-watchdog` flag
  before opening the serial control channel.
- After the fresh firmware epoch is observed, the runner appends one
  `ASTERINAS_PROBE_RECOVERY device=<id> status=<pass|fail|timeout|invalid>` line
  to the same per-device serial log; malformed evidence is recorded as
  `invalid` and is never promoted.
- `make test_riscv_megrez_hardware_probe_unit` passed 18 host contract tests;
  `make test_riscv_megrez_boot_menu_unit` passed 67 tests (including the boot
  manifest, RockOS attestation, menu, and Debian Stage1 contracts). The RISC-V `aster-kernel` check and
  kernel build both completed successfully in the persistent development
  container.
- On 2026-10-05, the strict validator was re-run against each retained
  physical transcript without touching the board. `dwc3-0`, `dwc3-1`, and
  `gmac-0` passed the fixed resource, interval, identity, whitelist, and
  recovery checks. `gmac-1` has no completion marker or register read after
  its DT marker, so it remains a timeout before the first MMIO access and is
  not a qualification. The four machine-readable reviews are retained as
  `strict-validator-review.json` inside each per-device evidence directory.
- The first runner rejected a valid `dwc3-0` log because it scanned U-Boot's
  own `Starting the controller` text. The original log and failed `result.json`
  are retained; the corrected interval-only validator produced
  `contract-review.json` and `reviewed-result.json` without rerunning hardware.
- The host contract now also accepts an explicit `ASTERINAS_PROBE_FAIL` with a
  whitelist-prefix of reads and a recovered firmware epoch. Such a run remains
  `status=fail`; it cannot be promoted to a device qualification or a passed
  prerequisite.
- A missing completion marker followed by a fresh firmware epoch is recorded
  as `probe_status=timeout` with `recovery_observed=true`; the runner does not
  promote that outcome to `passed` or `recovered`.
- The timeout path is now validated by the same host contract: it accepts only
  the resource/DT and whitelist-prefix emitted before recovery, and returns an
  explicit `status=timeout`. Replaying the retained GMAC-1 log yields that
  classification without touching the board.

## QEMU model evidence

The host binary requires GLIBC 2.38 while the workstation provides an older
runtime. Running it in the existing `asterinas-env:uboot-sim` container
with `QTEST_QEMU_BINARY=/qemu/build/qemu-system-riscv64` produced nine TAP
passes: four DWC3 tests (identity/isolation, glue reset, event-DMA/PLIC IRQ,
IRQ assertion) plus bulk loopback, two GMAC normal-path tests
(identity/MDIO/reset and descriptor loopback), and a GMAC-1 clock-gated
fail-closed test. The ninth test drives the exact GMAC-1 platform sequence
through the QEMU HSP/SYSCRG glue and verifies that the gated port becomes
readable only after all expected writes. The exact transcript is retained at
`target/megrez-qemu-hardware-probe-20261005/eic7700-dwc3-test-platform-sequence-rerun.tap`
with SHA-256
`4e123982b00c2678b7af9f9e0cc6c31c2884ccbe074e6afef03a340fd3a89fd3`.
The platform-sequence test also verifies that the port remains unreadable after
reset assertion and becomes readable only after the final reset-release mask.

This validates the QEMU controller model, its bounded loopback paths, and the
non-blocking clock-gated model; it does not qualify the physical GMAC-1 clock
and reset sequence.

The Asterinas kernel's fail-closed path was also exercised under the generic
QEMU `virt` machine with `asterinas.hardware_probe=gmac-1`. Because that
machine has no EIC7700 GMAC device tree, the probe rejected the resource before
the first MMIO read and emitted:

```text
ASTERINAS_PROBE_BEGIN device=gmac-1
ASTERINAS_PROBE_FAIL device=gmac-1 reason=invalid-device-tree
ASTERINAS_PROBE_DONE device=gmac-1 status=fail
ASTERINAS_PROBE_AUTO_REBOOT
```

The Stage1 recovery markers were present as well, and QEMU exited cleanly
after the bounded reboot. The complete serial log and machine-readable result
are retained under
`target/megrez-qemu-hardware-probe-20261005/asterinas-failure/`; the log
SHA-256 is
`5a8725933b8fd1fbd7f64473ff904a2d97dda5c304b44a7d2243dc68be80c7c8`.
This proves the kernel's explicit failure and recovery protocol, but it is a
generic-DT test rather than a reproduction of the physical GMAC-1 clock-gated
stall.

With the EIC7700 QEMU model's `eic7700-gmac1-clock-gated=on` property, the
same kernel reached the complete GMAC device-tree contract, read offset
`0x0000` as `0xffffffff`, and failed closed with
`reason=readback-mismatch`. Stage1 emitted its recovery marker and three fresh
OpenSBI epochs were observed during the bounded run. The raw serial log and
machine-readable result are under
`target/megrez-qemu-hardware-probe-20261005/asterinas-gmac1-gated/`; the log
SHA-256 is
`8428f9735c757f984ec8e4065e62299cfdc8b331ede5f4fc7df4b8bdc3d6e8ac`.
This is the first QEMU reproduction of the safe failure classification; it
still does not model a truly stalled MMIO transaction.

QEMU's synchronous `MemoryRegion` read callback cannot represent an infinite
interconnect stall without making the QEMU test process itself unrecoverable.
The physical GMAC-1 run is therefore the authoritative no-response evidence:
the hardware watchdog returned to firmware, while the host classified the
probe as `timeout` and did not promote it. A future GMAC-1 experiment must
first have an independently reviewed platform-clock/reset step; it is outside
this read-only Probe phase.

The current RISC-V image was rebuilt without staging it to the board. Its
SHA-256 is
`51c335bd2172f71a9307268b60d0a899e779c989eec705c346ca580fdfa3014e`.
`cargo check -p aster-kernel --target riscv64imac-unknown-none-elf` and the
full `make kernel TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode` both
completed successfully; only pre-existing warnings remain.

## Physical results

| Device | Result | Read values | Recovery |
| --- | --- | --- | --- |
| `dwc3-0` | pass | `0x0220fe45`, `0x00192004`, `0x5533330b` | OpenSBI/U-Boot; 37.8 s cycle |
| `dwc3-1` | pass | `0x0220fe45`, `0x00191004`, `0x5533330b` | OpenSBI/U-Boot; 37.7 s cycle |
| `gmac-0` | pass | `0x00000000`, `0x00000008`, `0x00001052`, `0x21008145`, `0x00000000`, `0x00000000` | OpenSBI/U-Boot; 37.9 s cycle |
| `gmac-1` | safe failure | no register value after the DT marker | watchdog returned to the default RockOS menu; no retry |

The per-run raw logs and results are under
`target/megrez-physical-step1/probe-{dwc3-0,dwc3-1,gmac-0,gmac-1}-20261005/`.
The `gmac-1` outcome is not a device qualification: it shows that enabling
platform clocks/resets would be required before any further GMAC1 access, which
is outside this read-only goal. No physical probe is approved until that path
has a separately reviewed, reversible clock/reset experiment. The static
boundary analysis is recorded in
`docs/porting/evidence/2026-10-05-gmac1-clock-gate-analysis.md`.
