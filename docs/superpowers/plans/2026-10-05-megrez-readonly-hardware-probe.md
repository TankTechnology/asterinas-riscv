# Megrez read-only hardware Probe implementation plan

**Goal:** Qualify USB0, then USB1, then individual GMACs with one fixed read-only
snapshot per boot and a verified recovery to RockOS.

**Architecture:** An explicit `asterinas.hardware_probe=<device>` kernel selector
is parsed once by `aster-cmdline`; both USB and GMAC observe it and suppress their
normal initialization even when the other component owns the selected device. The
early diagnostic path validates one fixed DT resource/IRQ tuple, reads only fixed
32-bit registers, emits serial markers, and lets the existing Stage1 deadline
request the recovery reboot. Host preparation creates immutable per-device menu
entries preserving RockOS as default, and requires hardware WDT readback before
loading. No user supplied address, register write, IRQ registration, DMA, PHY
transaction or clock/reset switch is exposed by the diagnostic code.

**Limits:** A software deadline cannot cancel a stalled MMIO transaction. Hardware
watchdog reset remains a separate physical contract; QEMU recovery alone does not
prove recovery from an EIC7700 interconnect lockup. Unknown clock/reset readiness
must prevent physical access rather than cause speculative clock writes.

## Tasks

- [x] Add pure Rust selection/register-contract tests. Reject unknown/multiple
  devices, malformed nonce, incorrect resources, zero/all-ones IDs and failed reads.
  Assert the reader performs the exact whitelist, stopping at the first failure.
- [x] Require every device marker, including `DONE`, to remain inside the single
  `BEGIN`/completion interval; reject stale markers from an earlier boot epoch.
- [x] Validate watchdog-recovered, incomplete probes as an explicit `timeout`
  result with only a whitelist-prefix and a fresh firmware epoch; never promote
  that result to a device pass.
- [x] Implement the kernel diagnostic path and fail-closed USB/GMAC suppression.
  Arm software recovery first, probe outside locks, record BEGIN/RESOURCE/READ/DONE/
  REBOOT markers, then emergency restart. Leave ordinary boots unaffected.
- [x] Add host preparation, immutable per-device canary menus and bounded runner.
  Preserve RockOS/default selector, verify artifact identity before each boot,
  arm hardware watchdog, record full serial logs and observed recovery independently
  of probe success. Require prior USB0/USB1 recovered passes before GMAC runs.
- [x] Make the hardware watchdog mandatory for every physical device-probe cycle;
  reject a missing watchdog flag before opening the serial control channel.
- [x] Append one host-observed `ASTERINAS_PROBE_RECOVERY` marker to each
  per-device transcript after a fresh firmware epoch is confirmed, including
  an `invalid` status when the recovered transcript fails strict validation.
- [x] Verify pure contracts and the EIC7700 QEMU model's normal identity,
  reset, IRQ, bounded loopback, and clock-gated fail-closed paths. The
  nine-test TAP transcript is
  retained under `target/megrez-qemu-hardware-probe-20261005/`.
- [x] Exercise the Asterinas kernel's explicit failure and recovery path under
  generic QEMU (`invalid-device-tree`); the current EIC7700 model still does
  not reproduce a clock-gated GMAC-1 stall or a stalled MMIO deadline.
- [x] Add an EIC7700-specific QEMU clock-gating scenario and exercise the
  Asterinas kernel's fail-closed readback path. The model returns an explicit
  unavailable value without blocking QEMU; the kernel classifies it as
  `readback-mismatch` and Stage1 recovery is observed.
- [x] Model the GMAC-1 HSP/SYSCRG platform-readiness sequence in QEMU and
  verify that only the complete ordered sequence makes the gated version
  register readable. GPIO/RGMII and physical writes remain out of scope.
- [x] Assess a genuinely stalled MMIO QEMU model. QEMU's synchronous
  `MemoryRegion` callback cannot provide an infinite stall and remain
  recoverable, so the physical GMAC-1 timeout is retained as the authoritative
  no-response evidence; no unsafe synthetic deadlock was added.
- [x] Re-read the RockOS clock/reset baseline with authenticated root read-only
  access and audit the exact source/artifact/register contracts.
- [x] Run USB0, restore RockOS, verify recovery, then run USB1 and the individual
  GMAC snapshots. GMAC-1 remains a recovered failed-safe result, not a device
  qualification.
- [x] Save source/artifact hashes, elapsed times and logs in the completion
  audit. Recovered failed probes are never counted as successful qualifications.

## Verification commands

Pure reader tests compile the production contract using `rustc --test` through
`tools/riscv/tests/test_megrez_hardware_probe.py`. Host contract tests use
`python3 -m unittest tools.riscv.tests.test_megrez_hardware_probe -v`.
The kernel build uses `tools/docker/run_dev_container.sh -- make kernel
TARGET_ARCH=riscv64 SMP=4 FEATURES=riscv_sv39_mode`.
QEMU runs and board runs have distinct evidence roots under
`target/megrez-hardware-probe/`; no physical invocation may retry implicitly.
