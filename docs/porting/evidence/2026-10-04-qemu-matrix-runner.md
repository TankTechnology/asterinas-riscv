# QEMU recovery matrix runner

Date: 2026-10-04 (Asia/Shanghai)

The recovery matrix is now available as `make test_riscv_uboot_recovery_matrix`.
It reuses prepared `qemu-virt` and `sifive-u` bundles, executes the five
registered device legs sequentially, continues after a failed leg, never
retries implicitly, and writes each leg's `serial.log`, `marker-event.txt`,
and `result.json` below a new output directory. `matrix.json` records the
return code and failure classification for every leg. A non-empty output
directory is rejected so an old result cannot be overwritten.

Validation completed without touching the physical board:

* `test_qemu_uboot_matrix` passed 10 tests.
* The combined QEMU contract/controller/matrix suite passed 262 tests with
  one environment-dependent skip.
* The DWMAC RX/TX DMA liveness and register-contract model passed 20 tests;
  the GMAC DTB/boot gate contract passed 45 tests.
* Ruff and `git diff --check` passed for the new runner and tests.
* An initial host-side invocation found no `qemu-system-riscv64` and each leg
  failed before QEMU startup; it left a diagnostic matrix at
  `target/qemu-uboot/matrix-live-0s5flK/`. The runner now checks this
  prerequisite once and rejects the invocation before creating leg output.
* A persistent-container invocation was stopped before QEMU launch because
  another long-lived container command held the shared build lock. It created
  no leg evidence and left no QEMU process.

After switching both inputs to the audited timer-recovery bundles, the first
real matrix run passed all five legs:

| Leg | Recovery | QEMU network | Result |
| --- | ---: | --- | --- |
| qemu-virt-headless | 11.356 s | `-nic none` | PASS |
| qemu-virt-net | 11.281 s | virtio-net/slirp plus `-nic none` | PASS |
| qemu-virt-xhci | 11.043 s | `-nic none` | PASS |
| sifive-u-headless | 9.383 s | `-nic none` | PASS |
| sifive-u-gem | 11.417 s | built-in GEM plus `-nic user` | PASS |

The run is retained at
`target/qemu-uboot/matrix-container-20261004-r5/`. Every leg sent one
`booti`, observed a fresh recovery epoch, reported `failure=null`, and had
`cleanup_complete=true`. The earlier all-timeout matrix used ordinary smoke
bundles; its logs reached the userspace greeting but never emitted the timer
marker. The Make target now requires explicit recovery-bundle directories to
make the intended payload selection explicit. This is not content attestation:
the Make target checks file presence, while the runtime recovery contract
rejects a smoke payload that never emits the timer-ready marker.

The same five-leg invocation was then repeated twice with `--repeat 2`; all
10 runs passed and are retained at
`target/qemu-uboot/matrix-container-20261004-r6-repeat2/matrix.json`. Across
the two attempts, recovery times were stable at 11.30 s for QEMU virt
headless, 11.30 s for virtio-net, 11.06 s for xHCI, 9.40 s for SiFive-U MMC,
and 11.41 s for the SiFive-U GEM firmware leg. No QEMU process remained after
cleanup.

The container's QEMU 10.2.1 device inventory contains `virtio-net`, e1000,
pcnet, and the SiFive-U built-in GEM model, but no `dwmac`, `stmmac`, or
EIC7700 GMAC device. Its RISC-V machine list likewise contains no EIC7700
board model. Consequently the GEM leg is the closest firmware/network
cross-check; interrupt, clock, DMA descriptor, and PHY behavior of the actual
EIC7700 GMAC cannot be established in QEMU alone.

The capability-aware runner was exercised on a headless recovery leg and
recorded the full inventory in
`target/qemu-uboot/matrix-container-20261004-r8-capability-v2/`:
`gmac_model_present=false`, QEMU version `10.2.1`, and a zero-error device
help probe. The result uses matrix schema v2; future QEMU upgrades will update
this field automatically before any matrix leg runs.

The fault-log extension was then exercised on a passing headless leg at
`target/qemu-uboot/matrix-container-20261004-r9-fault-logs/`. Its earlier
schema v3 summary indexes `runner.stdout.log` and `runner.stderr.log` beside
the guest serial/result evidence. A runner exception remains recoverable as a
file even when QEMU fails before producing `result.json`.

The current schema at that point was v4. It adds a bounded `diagnosis` field while preserving
the raw runner failure: boot timeout before `Starting kernel`, recovery-marker
absence after the kernel starts, missing recovery epochs, and runner launch
errors are distinguished. A fresh one-leg validation at
`target/qemu-uboot/matrix-container-20261004-r10-diagnosis-v4/` passed and
recorded `diagnosis=null`, QEMU 10.2.1, and `gmac_model_present=false`.

Replaying the current classifier over all five retained r3 failure logs
classified them as `recovery-marker-missing-after-kernel`. Each original
result remains failed with `boot-timeout`, exactly one `booti`, and complete
cleanup. This checks classification against recorded QEMU failures without
launching another deliberate timeout run; it is offline replay, not a new
runtime experiment.

The v4 matrix subsequently passed all five legs twice at
`target/qemu-uboot/matrix-container-20261004-r11-v4-repeat2/matrix.json`.
All ten per-leg results contain exactly one `booti`, completed software
recovery, complete cleanup, and no failure or diagnosis. Serial, marker,
runner stdout/stderr evidence exists for every leg. Boot disk, manifest,
payload DTB and U-Boot hashes match before and after each run. Recovery
intervals range from 9.348 to 11.408 seconds. No QEMU process remained.

Requirement audit:

| Requirement | Evidence and limit |
| --- | --- |
| Minimal UART/timer/MMC boot and recovery | SiFive-U headless repeats pass with `-nic none`; built-in GEM still exists in the model, but no network backend or firmware DHCP fallback is used. |
| Add USB independently | QEMU virt xHCI repeats pass; serial evidence shows Asterinas selects and starts PCI xHCI. This does not qualify EIC7700 DWC3 clocks/reset/DMA. |
| Add GMAC independently | Incomplete. SiFive GEM is a firmware cross-check; Asterinas does not bind its EIC7700 DWMAC driver to it. No EIC7700 model is available. |
| Timeout, recovery epochs, retained failure, no implicit retry | Current runner/controller unit coverage plus passing recovery matrix and offline classification of five retained timeout runs. |

The goal remains incomplete at the GMAC/hardware-isolation requirement.

The observation-aware runner now uses schema v5 and was exercised once across all five legs at
`target/qemu-uboot/matrix-container-20261004-r15-v5-usb-observations/matrix.json`.
It recorded `xhci-host-started` and `xhci-kernel-selected` only on the xHCI
leg, `gem-uboot-network-active` only on the GEM leg, and
`minimal-network-silent` plus `usb-disabled` on the SiFive-U headless leg.
The QEMU virt headless leg also recorded `usb-disabled`; the network leg
recorded only the recovery marker because it intentionally carries virtio-net.
All five legs passed and recorded the timer-recovery marker. These labels are
derived from serial evidence and deliberately do not claim EIC7700 GMAC
support.
Schema v5 adds these observations to the v4 failure/diagnosis record without
changing the raw runner failure semantics.

Finally, a deliberate one-leg negative run used the ordinary smoke bundle at
`target/qemu-uboot/current/` and was retained at
`target/qemu-uboot/matrix-container-20261004-r14-failure-v5/`. It timed out
after the kernel started without the timer marker, producing
`failure=boot-timeout` and
`diagnosis=recovery-marker-missing-after-kernel`. The result recorded exactly
one `booti`, `cleanup_complete=true`, `timed_out=true`, and retained serial,
marker, runner stdout/stderr, and result files. The matrix did not retry the
leg, and no QEMU process remained afterward.

The previously recorded in-container QEMU legs remain the authoritative
runtime evidence. The new command is an orchestration and evidence-boundary
improvement; it does not add EIC7700 GMAC, DWC3, clock, reset, or DMA models.
